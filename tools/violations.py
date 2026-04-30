"""
Tool: get_property_distress_signals
Hits NYC HPD Housing Maintenance Code Violations API.
Returns a structured distress assessment for a given BBL.

BBL = Borough-Block-Lot, NYC's unique identifier for every tax lot.
Format: 1-digit borough + 5-digit block + 4-digit lot = 10 digits total.
Example: 2026140035 = Brooklyn (3), block 00842, lot 0075.
"""
import json
import requests
from typing import Optional

HPD_VIOLATIONS_ENDPOINT = "https://data.cityofnewyork.us/resource/wvxf-dwi5.json"
REQUEST_TIMEOUT = 10  # seconds


def _classify_distress(total: int, class_c: int) -> str:
    """Derive a simple distress score from violation counts.

    Class C = immediately hazardous (the worst).
    Heuristic anyone can argue with — the point is the agent
    has a defensible signal, not a perfect one.
    """
    if class_c >= 3 or total >= 20:
        return "high"
    if class_c >= 1 or total >= 10:
        return "medium"
    if total > 0:
        return "low"
    return "none"


def get_property_distress_signals(bbl: str) -> str:
    """Fetch HPD violations for a BBL and return a distress summary as JSON.

    Args:
        bbl: 10-digit NYC Borough-Block-Lot identifier (string).

    Returns:
        JSON string with: bbl, open_violations_count, severity_breakdown,
        most_recent_violation, distress_score, and either a sample of
        violations or an error/empty marker.
    """
    # Input validation — handle empty / bad input before hitting the API
    if not bbl or not bbl.strip():
        return json.dumps({
            "error": "missing_bbl",
            "message": "BBL is required (10-digit string).",
        })

    bbl = bbl.strip()

    if not (bbl.isdigit() and len(bbl) == 10):
        return json.dumps({
            "bbl": bbl,
            "error": "invalid_bbl_format",
            "message": "BBL must be exactly 10 digits (1 borough + 5 block + 4 lot).",
        })

    try:
        response = requests.get(
            HPD_VIOLATIONS_ENDPOINT,
            params={
                "bbl": bbl,
                "$limit": 100,
                "$order": "novdescription DESC",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        violations = response.json()
    except requests.exceptions.Timeout:
        return json.dumps({
            "bbl": bbl,
            "error": "timeout",
            "message": f"NYC HPD API did not respond within {REQUEST_TIMEOUT}s.",
        })
    except requests.exceptions.RequestException as e:
        return json.dumps({
            "bbl": bbl,
            "error": "request_failed",
            "message": str(e),
        })
    except ValueError:
        return json.dumps({
            "bbl": bbl,
            "error": "invalid_response",
            "message": "HPD API returned non-JSON.",
        })

    source_url = f"https://data.cityofnewyork.us/Housing-Development/Housing-Maintenance-Code-Violations/wvxf-dwi5?bbl={bbl}"

    # Empty result handling — valid BBL, no violations on file
    if not violations:
        return json.dumps({
            "bbl": bbl,
            "open_violations_count": 0,
            "severity_breakdown": {"A": 0, "B": 0, "C": 0, "I": 0},
            "most_recent_violation": None,
            "distress_score": "none",
            "sample_violations": [],
            "source_url": source_url,
            "note": "No HPD violations on file for this BBL.",
        })

    # Aggregate
    severity = {"A": 0, "B": 0, "C": 0, "I": 0}
    most_recent = None
    sample = []

    for v in violations:
        cls = (v.get("class") or "").upper()
        if cls in severity:
            severity[cls] += 1

        inspection_date = v.get("inspectiondate") or v.get("novissueddate")
        if inspection_date and (most_recent is None or inspection_date > most_recent):
            most_recent = inspection_date

        if len(sample) < 5:
            sample.append({
                "class": cls,
                "description": (v.get("novdescription") or "")[:200],
                "inspection_date": inspection_date,
                "current_status": v.get("currentstatus", ""),
            })

    return json.dumps({
        "bbl": bbl,
        "open_violations_count": len(violations),
        "severity_breakdown": severity,
        "most_recent_violation": most_recent,
        "distress_score": _classify_distress(len(violations), severity["C"]),
        "sample_violations": sample,
        "source_url": source_url,
    }, indent=2)


if __name__ == "__main__":
    # Standalone smoke test — runs when you do `python tools/violations.py`
    print("=== Test 1: real BBL with likely violations ===")
    print(get_property_distress_signals("2026140035"))

    print("\n=== Test 2: empty input ===")
    print(get_property_distress_signals(""))

    print("\n=== Test 3: malformed BBL ===")
    print(get_property_distress_signals("not-a-bbl"))
