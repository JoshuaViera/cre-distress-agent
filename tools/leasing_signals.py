"""
Tool: get_leasing_signals
Reads local lease-comps CSV data and returns an observed market rent signal.

This is the practical v1 bridge between the demo-only hardcoded rent and paid
leasing datasets. Analysts can export comps from an approved source to CSV;
the agent filters those rows to the target deal and computes observed rent
deterministically before the LLM sees anything.
"""
from __future__ import annotations

import csv
import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


REQUIRED_COLUMNS = {
    "address",
    "submarket",
    "asset_class",
    "lease_date",
    "rent_psf",
    "square_feet",
    "source",
}


def _parse_date(raw: str) -> Optional[date]:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def _parse_float(raw: str) -> Optional[float]:
    cleaned = (raw or "").replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(statistics.median(values), 2)


def _weighted_average(rows: list[dict]) -> Optional[float]:
    weighted_sum = 0.0
    total_sf = 0.0
    for row in rows:
        rent = row["rent_psf"]
        sf = row["square_feet"] or 0.0
        if sf <= 0:
            continue
        weighted_sum += rent * sf
        total_sf += sf
    if total_sf <= 0:
        return None
    return round(weighted_sum / total_sf, 2)


def get_leasing_signals(
    deal_profile: dict,
    comps_csv_path: str,
    days_back: int = 90,
) -> str:
    """Return a rent signal from local lease comps.

    Args:
        deal_profile: parsed deal JSON with property.submarket and asset_class.
        comps_csv_path: path to a CSV with REQUIRED_COLUMNS.
        days_back: only include leases signed in this lookback window.

    Returns:
        JSON string with observed_rent_psf, comp_count, sample_comps, and
        source_url. Errors are returned as envelopes instead of exceptions.
    """
    prop = deal_profile.get("property") or {}
    target_submarket = (prop.get("submarket") or "").strip().lower()
    target_asset_class = (prop.get("asset_class") or "").strip().lower()

    path = Path(comps_csv_path).expanduser()
    if not path.exists():
        return json.dumps({
            "rent_signal": "unavailable",
            "error": "missing_comps_csv",
            "message": f"Lease comps CSV not found at {comps_csv_path}.",
            "observed_rent_psf": None,
            "source_url": f"local_csv:{comps_csv_path}",
        })

    if not isinstance(days_back, int) or days_back < 1:
        return json.dumps({
            "rent_signal": "unavailable",
            "error": "invalid_days_back",
            "message": "days_back must be a positive integer.",
            "observed_rent_psf": None,
            "source_url": f"local_csv:{path}",
        })

    cutoff = date.today() - timedelta(days=days_back)
    rows: list[dict] = []

    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            headers = set(reader.fieldnames or [])
            missing = sorted(REQUIRED_COLUMNS - headers)
            if missing:
                return json.dumps({
                    "rent_signal": "unavailable",
                    "error": "missing_columns",
                    "message": f"Lease comps CSV is missing columns: {', '.join(missing)}.",
                    "observed_rent_psf": None,
                    "source_url": f"local_csv:{path}",
                })

            for raw in reader:
                lease_date = _parse_date(raw.get("lease_date", ""))
                rent_psf = _parse_float(raw.get("rent_psf", ""))
                square_feet = _parse_float(raw.get("square_feet", "")) or 0.0
                if lease_date is None or rent_psf is None:
                    continue
                if lease_date < cutoff:
                    continue
                if (raw.get("submarket") or "").strip().lower() != target_submarket:
                    continue
                if (raw.get("asset_class") or "").strip().lower() != target_asset_class:
                    continue

                rows.append({
                    "address": (raw.get("address") or "").strip(),
                    "submarket": (raw.get("submarket") or "").strip(),
                    "asset_class": (raw.get("asset_class") or "").strip(),
                    "lease_date": lease_date.isoformat(),
                    "rent_psf": rent_psf,
                    "square_feet": square_feet,
                    "tenant": (raw.get("tenant") or "").strip(),
                    "source": (raw.get("source") or "").strip(),
                })
    except OSError as exc:
        return json.dumps({
            "rent_signal": "unavailable",
            "error": "read_failed",
            "message": str(exc),
            "observed_rent_psf": None,
            "source_url": f"local_csv:{path}",
        })

    if not rows:
        return json.dumps({
            "rent_signal": "no_comps",
            "observed_rent_psf": None,
            "comp_count": 0,
            "median_rent_psf": None,
            "weighted_average_rent_psf": None,
            "sample_comps": [],
            "filters": {
                "submarket": prop.get("submarket"),
                "asset_class": prop.get("asset_class"),
                "days_back": days_back,
            },
            "source_url": f"local_csv:{path}",
            "note": "No matching lease comps after filtering.",
        }, indent=2)

    rents = [row["rent_psf"] for row in rows]
    weighted = _weighted_average(rows)
    median = _median(rents)
    observed = median

    rows.sort(key=lambda row: row["lease_date"], reverse=True)
    return json.dumps({
        "rent_signal": "available",
        "observed_rent_psf": observed,
        "comp_count": len(rows),
        "median_rent_psf": median,
        "weighted_average_rent_psf": weighted,
        "method": "same submarket + same asset class + last N days; observed rent uses median rent_psf",
        "sample_comps": rows[:5],
        "filters": {
            "submarket": prop.get("submarket"),
            "asset_class": prop.get("asset_class"),
            "days_back": days_back,
        },
        "source_url": f"local_csv:{path}",
    }, indent=2)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    deal_path = here.parent / "deals" / "midtown-south-office-001.json"
    comps_path = here.parent / "data" / "lease_comps_sample.csv"
    with open(deal_path) as f:
        deal = json.load(f)

    out = get_leasing_signals(deal, str(comps_path))
    parsed = json.loads(out)
    print(out)
    assert parsed["rent_signal"] == "available"
    assert parsed["observed_rent_psf"] == 68.0
    assert parsed["comp_count"] == 3
    print("[PASS] sample lease comps produce observed rent of $68.00/SF")
