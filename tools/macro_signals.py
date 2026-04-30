"""
Macro signals tool — Tool 3 of the CRE Deal Pulse agent.

Hits the FRED (Federal Reserve Economic Data) API for two series:
    DGS10 — 10-Year Treasury Constant Maturity Rate (daily, percent)
    SOFR  — Secured Overnight Financing Rate (daily, percent)

Returns current vs. prior values for each series, signed bps_change, and a
top-level macro_signal classification.

Sign convention:
    bps_change = current - prior (in basis points)
    Positive = rates rose (typically bad for the deal — higher cost of debt)
    Negative = rates fell (typically good for the deal)

Requires FRED_API_KEY in the environment. Get a free key at
https://fred.stlouisfed.org/docs/api/api_key.html. Missing key returns a
clean error envelope so the agent can narrate the failure rather than crash.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FRED_DOCS = "https://fred.stlouisfed.org/series/{series_id}"
REQUEST_TIMEOUT = 10  # seconds

# A move of 25+ bps in either direction is the threshold for "rates_moved"
MATERIAL_BPS_THRESHOLD = 25

# Series we watch
SERIES = {
    "treasury_10y": "DGS10",
    "sofr": "SOFR",
}


def _error(message: str) -> str:
    """Standard error envelope, matching the pattern used by violations.py."""
    return json.dumps({
        "macro_signal": "error",
        "error": message,
        "treasury_10y": None,
        "sofr": None,
    })


def _fetch_series(series_id: str, api_key: str, lookback_days: int) -> Optional[list[dict]]:
    """Pull recent observations for a FRED series. Returns list newest-first,
    or None on any failure (network, auth, parse).
    """
    end = datetime.now(timezone.utc).date()
    # Pad the window: FRED skips weekends/holidays, so lookback_days alone may
    # not contain enough trading days for a clean current/prior pair.
    start = end - timedelta(days=lookback_days + 21)

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "sort_order": "desc",
        "limit": 200,
    }
    try:
        resp = requests.get(FRED_BASE, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        observations = resp.json().get("observations", [])
    except Exception:
        return None

    # FRED uses "." for missing values
    valid = [o for o in observations if o.get("value") not in (None, "", ".", "null")]
    return valid or None


def _pair_current_prior(observations: list[dict], lookback_days: int) -> Optional[dict]:
    """Pick the most recent observation as current, then the first observation
    on or before (current_date - lookback_days) as prior.
    """
    if not observations:
        return None
    try:
        current = observations[0]
        current_date = datetime.strptime(current["date"], "%Y-%m-%d").date()
        target = current_date - timedelta(days=lookback_days)

        prior = None
        for obs in observations[1:]:
            obs_date = datetime.strptime(obs["date"], "%Y-%m-%d").date()
            if obs_date <= target:
                prior = obs
                break
        if prior is None:
            prior = observations[-1]

        current_value = float(current["value"])
        prior_value = float(prior["value"])
        return {
            "current_value": current_value,
            "prior_value": prior_value,
            "current_date": current["date"],
            "prior_date": prior["date"],
            "bps_change": round((current_value - prior_value) * 100),
        }
    except (ValueError, KeyError, TypeError):
        return None


def _classify(series_results: list[Optional[dict]]) -> str:
    """Top-level macro_signal: rates_moved / stable / no_data."""
    valid = [s for s in series_results if s is not None]
    if not valid:
        return "no_data"
    if any(abs(s["bps_change"]) >= MATERIAL_BPS_THRESHOLD for s in valid):
        return "rates_moved"
    return "stable"


def _narrate(treasury: Optional[dict], sofr: Optional[dict], days_back: int) -> str:
    """One-line plain-English summary the agent can quote."""
    pieces = []
    if treasury is not None:
        bps = treasury["bps_change"]
        word = "flat" if bps == 0 else (f"rose {bps} bps" if bps > 0 else f"fell {abs(bps)} bps")
        pieces.append(f"10Y {word} (now {treasury['current_value']}%)")
    if sofr is not None:
        bps = sofr["bps_change"]
        word = "flat" if bps == 0 else (f"rose {bps} bps" if bps > 0 else f"fell {abs(bps)} bps")
        pieces.append(f"SOFR {word} (now {sofr['current_value']}%)")
    if not pieces:
        return "No macro data available."
    return f"Over the last {days_back} days: " + "; ".join(pieces) + "."


def get_macro_signals(days_back: int = 30) -> str:
    """Fetch recent FRED rate data (10Y Treasury, SOFR) and return a JSON envelope.

    Args:
        days_back: How many calendar days back to compare against. Default 30.

    Returns:
        JSON string with:
          - macro_signal      ("rates_moved" | "stable" | "no_data" | "error")
          - as_of             (ISO date)
          - lookback_days     (echo input)
          - treasury_10y      ({current_value, prior_value, bps_change, ...} | null)
          - sofr              (same shape | null)
          - narrative         (one-line summary the briefing can quote)
          - source_url_*      (citable FRED links)
          - error / message   (only on failure)
    """
    if not isinstance(days_back, int) or days_back < 1:
        return _error("days_back must be a positive integer")

    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        return _error(
            "FRED_API_KEY is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html "
            "and add it to your .env."
        )

    treasury_obs = _fetch_series(SERIES["treasury_10y"], api_key, days_back)
    sofr_obs = _fetch_series(SERIES["sofr"], api_key, days_back)

    if treasury_obs is None and sofr_obs is None:
        return _error("FRED API returned no data for either DGS10 or SOFR.")

    treasury = _pair_current_prior(treasury_obs, days_back) if treasury_obs else None
    sofr = _pair_current_prior(sofr_obs, days_back) if sofr_obs else None

    if treasury is not None:
        treasury["series_id"] = SERIES["treasury_10y"]
    if sofr is not None:
        sofr["series_id"] = SERIES["sofr"]

    return json.dumps({
        "macro_signal": _classify([treasury, sofr]),
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "lookback_days": days_back,
        "treasury_10y": treasury,
        "sofr": sofr,
        "narrative": _narrate(treasury, sofr, days_back),
        "source_url_treasury_10y": FRED_DOCS.format(series_id=SERIES["treasury_10y"]),
        "source_url_sofr": FRED_DOCS.format(series_id=SERIES["sofr"]),
    }, indent=2)


# ─────────────────────────────────────────────────────────
# Standalone tests — run `python tools/macro_signals.py`
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    print("=" * 60)
    print("TEST 1: Missing FRED_API_KEY returns error envelope")
    print("=" * 60)
    saved = os.environ.pop("FRED_API_KEY", None)
    out = get_macro_signals()
    parsed = json.loads(out)
    print(out)
    assert parsed["macro_signal"] == "error"
    assert "FRED_API_KEY" in parsed["error"]
    print("[PASS] missing-key error envelope")
    if saved:
        os.environ["FRED_API_KEY"] = saved

    print("\n" + "=" * 60)
    print("TEST 2: Bad days_back returns error envelope")
    print("=" * 60)
    out = get_macro_signals(days_back=0)
    parsed = json.loads(out)
    print(out)
    assert parsed["macro_signal"] == "error"
    print("[PASS] bad input error envelope")

    if not os.getenv("FRED_API_KEY"):
        print("\n[SKIP] No FRED_API_KEY in env — skipping live FRED test.")
        print("       Set FRED_API_KEY in .env to run TEST 3.")
    else:
        print("\n" + "=" * 60)
        print("TEST 3: Live FRED call")
        print("=" * 60)
        out = get_macro_signals(days_back=30)
        parsed = json.loads(out)
        print(out)
        assert parsed["macro_signal"] in ("rates_moved", "stable")
        assert parsed["treasury_10y"] is not None or parsed["sofr"] is not None
        if parsed["treasury_10y"] is not None:
            assert isinstance(parsed["treasury_10y"]["bps_change"], int)
        if parsed["sofr"] is not None:
            assert isinstance(parsed["sofr"]["bps_change"], int)
        print(f"[PASS] live FRED — macro_signal: {parsed['macro_signal']}")

    print("\n" + "=" * 60)
    print("TEST 4: Sign-convention math sanity")
    print("=" * 60)
    actual = round((4.27 - 4.02) * 100)
    assert actual == 25
    print(f"[PASS] 4.27% - 4.02% = +{actual} bps (rates rose)")
    actual = round((3.85 - 4.10) * 100)
    assert actual == -25
    print(f"[PASS] 3.85% - 4.10% = {actual} bps (rates fell)")

    print("\nAll tests passed.")
