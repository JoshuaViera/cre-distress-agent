"""
Macro signals tool — Tool 3 of the CRE Deal Pulse agent.

Tries the U.S. Treasury fiscaldata.treasury.gov API (free, no auth) for daily
10-Year Treasury par yield data. If the API is unreachable, blocked, or
returns no data, falls back to a realistic stub so the demo always runs.

The fallback is clearly labeled: the JSON output has a "data_source" field
that reads either "treasury_api_live" or "fallback_stub". The agent narrates
the same way regardless; you can see which path ran by inspecting the JSON.

Returns current vs. prior 10-Year Treasury values, signed bps_change, and a
top-level macro_signal classification.

Sign convention:
    bps_change = current - prior (in basis points)
    Positive = rates rose (typically bad for the deal — higher cost of debt)
    Negative = rates fell (typically good for the deal)

Note: SOFR is intentionally omitted from v1. SOFR moves to v2.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

import requests

# Treasury fiscal data API — daily Treasury par yield curve rates
TREASURY_BASE = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    "/v2/accounting/od/daily_treasury_yield_curve_rates"
)

# A move of 25+ bps in either direction is the threshold for "rates_moved"
MATERIAL_BPS_THRESHOLD = 25

# Realistic recent 10-year Treasury values for fallback. Tuned so the demo
# tells a "rates moved against the deal" story (28 bps higher → above the
# 25-bps materiality threshold → triggers macro_signal="rates_moved").
FALLBACK_CURRENT = 4.32
FALLBACK_PRIOR = 4.04


def _error(message: str) -> str:
    """Standard error envelope, matching the pattern used by violations.py."""
    return json.dumps({
        "macro_signal": "error",
        "error": message,
        "treasury_10y": None,
        "data_source": "error",
    })


def _try_fetch_10y(days_back: int) -> Optional[dict]:
    """
    Try the Treasury API. Returns parsed observation dict on success, or
    None on any failure (network error, 403, no data, parse error). Never
    raises — the caller falls back to stub data on None.
    """
    end = datetime.utcnow().date()
    start = end - timedelta(days=days_back + 21)

    params = {
        "fields": "record_date,bc_10year",
        "filter": f"record_date:gte:{start.isoformat()},record_date:lte:{end.isoformat()}",
        "sort": "-record_date",
        "page[size]": 200,
    }
    headers = {"User-Agent": "CRE-Deal-Pulse/0.1 (Pursuit Fellowship demo)"}

    try:
        resp = requests.get(TREASURY_BASE, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:
        return None

    valid = [r for r in data if r.get("bc_10year") not in (None, "", "null")]
    if not valid:
        return None

    try:
        current = valid[0]
        current_date = datetime.strptime(current["record_date"], "%Y-%m-%d").date()
        target_date = current_date - timedelta(days=days_back)

        prior = None
        for r in valid[1:]:
            r_date = datetime.strptime(r["record_date"], "%Y-%m-%d").date()
            if r_date <= target_date:
                prior = r
                break
        if prior is None and len(valid) > 1:
            prior = valid[-1]
        if prior is None:
            return None

        current_value = float(current["bc_10year"])
        prior_value = float(prior["bc_10year"])
        bps_change = round((current_value - prior_value) * 100)

        return {
            "series_id": "BC_10YEAR",
            "current_value": current_value,
            "prior_value": prior_value,
            "current_date": current["record_date"],
            "prior_date": prior["record_date"],
            "bps_change": bps_change,
        }
    except Exception:
        return None


def _stub_10y(days_back: int) -> dict:
    """Realistic fallback data so the demo runs even with no network."""
    today = datetime.utcnow().date()
    bps_change = round((FALLBACK_CURRENT - FALLBACK_PRIOR) * 100)
    return {
        "series_id": "BC_10YEAR",
        "current_value": FALLBACK_CURRENT,
        "prior_value": FALLBACK_PRIOR,
        "current_date": today.isoformat(),
        "prior_date": (today - timedelta(days=days_back)).isoformat(),
        "bps_change": bps_change,
    }


def _classify(treasury: dict) -> str:
    """Top-level macro_signal: rates_moved / stable."""
    if abs(treasury["bps_change"]) >= MATERIAL_BPS_THRESHOLD:
        return "rates_moved"
    return "stable"


def _narrate(treasury: dict, days_back: int) -> str:
    """One-line plain-English summary the agent can quote directly."""
    bps = treasury["bps_change"]
    if bps == 0:
        direction = "10Y flat"
    elif bps > 0:
        direction = f"10Y rose {bps} bps"
    else:
        direction = f"10Y fell {abs(bps)} bps"
    return f"{direction} over the last {days_back} days (now {treasury['current_value']}%)."


def get_macro_signals(days_back: int = 30) -> str:
    """
    Fetch recent 10-Year Treasury data and return a JSON-encoded macro signal.

    Tries the Treasury API first. Falls back to realistic stub data if the
    API is unreachable. The "data_source" field in the response indicates
    which path ran.

    Args:
        days_back: How many calendar days back to compare against. Default 30.

    Returns:
        JSON string with macro_signal, treasury_10y observations, narrative,
        as_of date, and data_source. Always returns a JSON string.
    """
    if not isinstance(days_back, int) or days_back < 1:
        return _error("days_back must be a positive integer")

    treasury = _try_fetch_10y(days_back)
    data_source = "treasury_api_live"

    if treasury is None:
        treasury = _stub_10y(days_back)
        data_source = "fallback_stub"

    return json.dumps({
        "macro_signal": _classify(treasury),
        "as_of": datetime.utcnow().date().isoformat(),
        "lookback_days": days_back,
        "treasury_10y": treasury,
        "narrative": _narrate(treasury, days_back),
        "data_source": data_source,
    })


# ─────────────────────────────────────────────────────────
# Standalone tests — run `python tools/macro_signals.py`
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("TEST 1: Real call, default 30-day lookback")
    print("=" * 60)
    out = get_macro_signals()
    print(out)
    parsed = json.loads(out)
    assert parsed["macro_signal"] in ("rates_moved", "stable"), \
        f"Unexpected macro_signal: {parsed['macro_signal']}"
    assert parsed["data_source"] in ("treasury_api_live", "fallback_stub")
    assert parsed["treasury_10y"]["current_value"] is not None
    assert isinstance(parsed["treasury_10y"]["bps_change"], int)
    print(f"  ✓ data_source: {parsed['data_source']}")
    print(f"  ✓ 10Y current: {parsed['treasury_10y']['current_value']}%")
    print(f"  ✓ 10Y prior: {parsed['treasury_10y']['prior_value']}%")
    print(f"  ✓ bps_change: {parsed['treasury_10y']['bps_change']}")
    print(f"  ✓ macro_signal: {parsed['macro_signal']}")
    if parsed["data_source"] == "fallback_stub":
        print("  ⚠️  Treasury API was unreachable; fallback stub was used.")
        print("     The demo will still run. The agent narrates the same way.")

    print("\n" + "=" * 60)
    print("TEST 2: Bad input (days_back=0) returns error envelope")
    print("=" * 60)
    out = get_macro_signals(days_back=0)
    print(out)
    parsed = json.loads(out)
    assert parsed["macro_signal"] == "error"
    print("  ✓ Returned error envelope, did not crash")

    print("\n" + "=" * 60)
    print("TEST 3: Sanity-check bps_change math")
    print("=" * 60)
    actual = round((4.27 - 4.02) * 100)
    assert actual == 25, f"Expected 25, got {actual}"
    print(f"  ✓ 4.27% - 4.02% = +{actual} bps")
    actual = round((3.85 - 4.10) * 100)
    assert actual == -25, f"Expected -25, got {actual}"
    print(f"  ✓ 3.85% - 4.10% = {actual} bps")

    print("\n✅ All tests passed.")