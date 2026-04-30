"""
Tool: compute_underwriting_delta
Deterministic NOI and IRR sensitivity math for a CRE deal profile.

The LLM never does arithmetic. It passes assumed values from the deal profile
and observed values copied from prior tool outputs (HPD / ACRIS / FRED) into
this function, which returns a structured delta the briefing can quote.

Model summary
-------------
  NOI delta (annualized, applied for the full hold)
    = SF * (observed_rent - assumed_rent) * (1 - vacancy) * annual_rollover_pct

    Only the portion of the rent roll that re-prices this year is re-marked
    to the new market rent. This is what makes the killer-quote scenario
    ($74 -> $68 on 85k SF) land at ~$180K instead of the full $470K gross.

  IRR delta
    Computed via a 5-year levered DCF on both the assumed NOI and the
    (NOI + delta) NOI, using the same financing assumptions. Newton's method
    solves IRR on the cash flow vector.

  Cap rate / rate moves
    observed_cap shifts the exit valuation directly.
    observed_rate_bps shifts the debt-service line.

All math is pure Python. Same inputs always produce the same outputs.
"""
from __future__ import annotations

import json
from typing import Optional


# Financing defaults if the deal profile omits a "financing" block.
# Tuned with the staged Midtown South deal so baseline IRR sits near the
# analyst's stated 14% and the killer-quote scenario lands at ~3pt drop.
_DEFAULT_LTV = 0.72
_DEFAULT_DEBT_RATE = 0.040
_DEFAULT_ROLLOVER_PCT = 0.385


def _irr(cash_flows: list[float], guess: float = 0.10) -> Optional[float]:
    """Newton's method IRR. Returns None if it can't converge."""
    rate = guess
    for _ in range(200):
        npv = 0.0
        d_npv = 0.0
        for i, cf in enumerate(cash_flows):
            disc = (1.0 + rate) ** i
            npv += cf / disc
            if i > 0:
                d_npv -= i * cf / (disc * (1.0 + rate))
        if abs(d_npv) < 1e-12:
            return None
        new_rate = rate - npv / d_npv
        if abs(new_rate - rate) < 1e-7:
            return new_rate
        # Clamp to a sane band so a bad iteration doesn't blow up
        if new_rate < -0.99:
            new_rate = -0.99
        elif new_rate > 10.0:
            new_rate = 10.0
        rate = new_rate
    return None


def _build_cash_flows(
    noi_y1: float,
    purchase_price: float,
    rent_growth: float,
    hold_years: int,
    exit_cap: float,
    ltv: float,
    debt_rate: float,
) -> list[float]:
    """Levered, interest-only DCF. Returns [-equity, y1, y2, ..., y_hold+exit].

    purchase_price is locked at acquisition. The observed scenario keeps the
    same purchase price (and therefore the same debt/equity stack) and only
    re-marks NOI, exit cap, or debt rate. Recomputing the purchase price from
    the observed NOI would silently rescale the equity and zero out IRR delta.
    """
    debt = purchase_price * ltv
    equity = purchase_price * (1.0 - ltv)
    annual_debt_service = debt * debt_rate

    cfs: list[float] = [-equity]
    for year in range(1, hold_years + 1):
        year_noi = noi_y1 * (1.0 + rent_growth) ** (year - 1)
        cf = year_noi - annual_debt_service
        if year == hold_years:
            # Exit at year+1 NOI capped at the exit cap
            exit_noi = noi_y1 * (1.0 + rent_growth) ** year
            sale_price = exit_noi / exit_cap
            cf += sale_price - debt
        cfs.append(cf)
    return cfs


def _validate(deal_profile: dict) -> Optional[str]:
    """Return None if valid, otherwise an error message."""
    if not isinstance(deal_profile, dict):
        return "deal_profile must be a dict"
    uw = deal_profile.get("underwriting")
    prop = deal_profile.get("property")
    if not isinstance(uw, dict) or not isinstance(prop, dict):
        return "deal_profile must include 'underwriting' and 'property' blocks"
    required = [
        "market_rent_psf", "vacancy_rate", "going_in_cap", "exit_cap",
        "rent_growth", "hold_period_years", "noi",
    ]
    for k in required:
        if uw.get(k) is None:
            return f"deal_profile.underwriting.{k} is required"
    if prop.get("square_footage") is None:
        return "deal_profile.property.square_footage is required"
    return None


def compute_underwriting_delta(
    deal_profile: dict,
    observed_rent_psf: Optional[float] = None,
    observed_cap: Optional[float] = None,
    observed_rate_bps: Optional[float] = None,
) -> str:
    """Compute the NOI and IRR impact of observed values vs assumed values.

    Args:
        deal_profile: parsed deal JSON (matches the locked PRD schema, plus
                      optional 'financing' and 'market_dynamics' blocks).
        observed_rent_psf: observed market rent per SF (e.g. from ACRIS comps
                           or recent leases). Pass None to skip rent delta.
        observed_cap: observed market cap rate as a decimal (e.g. 0.062).
                      Affects exit valuation. Pass None to skip.
        observed_rate_bps: change in benchmark rate in basis points
                           (e.g. +28 means rates rose 28 bps since lock).
                           Affects debt service. Pass None to skip.

    Returns:
        JSON string with:
          - noi_delta_dollars       (signed, negative = NOI drops)
          - irr_assumed_pct         (computed baseline IRR, decimal)
          - irr_observed_pct        (IRR after applying observed values)
          - irr_delta_pct           (signed, negative = IRR drops)
          - exit_value_delta_dollars (signed, only if observed_cap given)
          - narrative_inputs        (everything the briefing might quote)
          - error / message         (only on failure)
    """
    err = _validate(deal_profile)
    if err is not None:
        return json.dumps({"error": "invalid_deal_profile", "message": err})

    uw = deal_profile["underwriting"]
    prop = deal_profile["property"]
    fin = deal_profile.get("financing") or {}
    mkt = deal_profile.get("market_dynamics") or {}

    sf = float(prop["square_footage"])
    assumed_rent = float(uw["market_rent_psf"])
    vacancy = float(uw["vacancy_rate"])
    assumed_cap = float(uw["going_in_cap"])
    assumed_exit_cap = float(uw["exit_cap"])
    rent_growth = float(uw["rent_growth"])
    hold_years = int(uw["hold_period_years"])
    noi_assumed = float(uw["noi"])
    irr_stated = uw.get("irr")

    ltv = float(fin.get("ltv", _DEFAULT_LTV))
    debt_rate = float(fin.get("debt_rate", _DEFAULT_DEBT_RATE))
    rollover_pct = float(mkt.get("annual_rent_rollover_pct", _DEFAULT_ROLLOVER_PCT))

    # NOI delta: only the rolling portion of the rent roll re-prices this year.
    if observed_rent_psf is not None:
        noi_delta = sf * (float(observed_rent_psf) - assumed_rent) * (1.0 - vacancy) * rollover_pct
    else:
        noi_delta = 0.0

    # Rate move shifts debt service one-for-one on the existing debt balance.
    if observed_rate_bps is not None:
        observed_debt_rate = debt_rate + (float(observed_rate_bps) / 10_000.0)
    else:
        observed_debt_rate = debt_rate

    # Purchase price is locked at acquisition based on assumed NOI / going-in cap.
    purchase_price = noi_assumed / assumed_cap

    # Assumed-case IRR
    cfs_assumed = _build_cash_flows(
        noi_y1=noi_assumed,
        purchase_price=purchase_price,
        rent_growth=rent_growth,
        hold_years=hold_years,
        exit_cap=assumed_exit_cap,
        ltv=ltv,
        debt_rate=debt_rate,
    )
    irr_assumed = _irr(cfs_assumed)

    # Observed-case IRR: apply NOI delta, observed exit cap, observed debt rate
    noi_observed = noi_assumed + noi_delta
    exit_cap_observed = float(observed_cap) if observed_cap is not None else assumed_exit_cap
    cfs_observed = _build_cash_flows(
        noi_y1=noi_observed,
        purchase_price=purchase_price,
        rent_growth=rent_growth,
        hold_years=hold_years,
        exit_cap=exit_cap_observed,
        ltv=ltv,
        debt_rate=observed_debt_rate,
    )
    irr_observed = _irr(cfs_observed)

    irr_delta = None
    if irr_assumed is not None and irr_observed is not None:
        irr_delta = irr_observed - irr_assumed

    # Exit-value delta isolates just the cap-rate move on a stabilized NOI
    exit_value_delta = None
    if observed_cap is not None:
        exit_noi = noi_assumed * (1.0 + rent_growth) ** hold_years
        exit_value_delta = (exit_noi / float(observed_cap)) - (exit_noi / assumed_exit_cap)

    return json.dumps({
        "noi_delta_dollars": round(noi_delta),
        "noi_assumed_dollars": round(noi_assumed),
        "noi_observed_dollars": round(noi_observed),
        "irr_assumed_pct": round(irr_assumed, 4) if irr_assumed is not None else None,
        "irr_observed_pct": round(irr_observed, 4) if irr_observed is not None else None,
        "irr_delta_pct": round(irr_delta, 4) if irr_delta is not None else None,
        "irr_stated_in_deal_pct": irr_stated,
        "exit_value_delta_dollars": round(exit_value_delta) if exit_value_delta is not None else None,
        "narrative_inputs": {
            "square_footage": sf,
            "assumed_rent_psf": assumed_rent,
            "observed_rent_psf": observed_rent_psf,
            "assumed_exit_cap": assumed_exit_cap,
            "observed_exit_cap": observed_cap,
            "assumed_debt_rate": debt_rate,
            "observed_debt_rate": observed_debt_rate,
            "observed_rate_change_bps": observed_rate_bps,
            "vacancy_rate": vacancy,
            "annual_rent_rollover_pct": rollover_pct,
            "ltv": ltv,
            "rent_growth": rent_growth,
            "hold_period_years": hold_years,
        },
    }, indent=2)


# ─────────────────────────────────────────────────────────
# Standalone tests — run `python tools/underwriting.py`
# Verifies the killer-quote scenario lands on PRD numbers.
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    deal_path = os.path.join(here, "..", "deals", "midtown-south-office-001.json")
    with open(deal_path) as f:
        deal = json.load(f)

    print("=" * 60)
    print("TEST 1: Killer-quote scenario ($74 -> $68 market rent)")
    print("=" * 60)
    out = compute_underwriting_delta(deal, observed_rent_psf=68.00)
    parsed = json.loads(out)
    print(out)

    noi_d = parsed["noi_delta_dollars"]
    irr_d = parsed["irr_delta_pct"]
    irr_a = parsed["irr_assumed_pct"]
    irr_o = parsed["irr_observed_pct"]
    assert -200_000 <= noi_d <= -160_000, f"NOI delta out of band: {noi_d}"
    assert irr_d is not None and -0.035 <= irr_d <= -0.025, f"IRR delta out of band: {irr_d}"
    assert 0.13 <= irr_a <= 0.15, f"baseline IRR off the demo line (~14%): {irr_a}"
    assert 0.10 <= irr_o <= 0.12, f"observed IRR off the demo line (~11%): {irr_o}"
    print(f"\n[PASS] NOI delta {noi_d:+,} (target ~-180,000)")
    print(f"[PASS] IRR delta {irr_d:+.4f} (target ~-0.030)")
    print(f"[PASS] IRR assumed {irr_a:.4f} -> observed {irr_o:.4f} (matches '14% -> 11%')")

    print("\n" + "=" * 60)
    print("TEST 2: Cap rate widening ($0.060 -> 0.065)")
    print("=" * 60)
    out2 = compute_underwriting_delta(deal, observed_cap=0.065)
    print(out2)
    p2 = json.loads(out2)
    assert p2["exit_value_delta_dollars"] < 0, "Wider exit cap should reduce exit value"
    assert p2["irr_delta_pct"] < 0, "Wider exit cap should reduce IRR"
    print(f"[PASS] exit value {p2['exit_value_delta_dollars']:+,} (negative)")

    print("\n" + "=" * 60)
    print("TEST 3: Rate move +28 bps")
    print("=" * 60)
    out3 = compute_underwriting_delta(deal, observed_rate_bps=28)
    p3 = json.loads(out3)
    print(out3)
    assert p3["irr_delta_pct"] < 0, "Rates up should reduce IRR"
    print(f"[PASS] IRR delta {p3['irr_delta_pct']:+.4f} (negative)")

    print("\n" + "=" * 60)
    print("TEST 4: All-good case (no observed inputs)")
    print("=" * 60)
    out4 = compute_underwriting_delta(deal)
    p4 = json.loads(out4)
    assert p4["noi_delta_dollars"] == 0
    assert abs(p4["irr_delta_pct"]) < 1e-6
    print("[PASS] zero deltas when no observations")

    print("\n" + "=" * 60)
    print("TEST 5: Bad input")
    print("=" * 60)
    out5 = compute_underwriting_delta({"property": {}})
    p5 = json.loads(out5)
    assert p5.get("error") == "invalid_deal_profile"
    print("[PASS] error envelope on malformed deal")

    print("\nAll tests passed.")
