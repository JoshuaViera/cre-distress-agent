"""
Tool: get_market_signals
Hits NYC ACRIS (Automated City Register Information System) via Socrata.

Strategy:
  1. Query ACRIS Master (bnx9-e6tj) for DEED / DEEDO documents in the
     target borough within the lookback window, filtered by min sale price.
  2. Grab the matching document_ids, then query ACRIS Legals (8h5j-fqxa)
     to confirm borough + resolve address — Socrata doesn't support a true
     cross-dataset JOIN, so we do it in two round trips.
  3. Aggregate: sale count, median price, 5-record sample, market_signal.

Borough name → ACRIS recorded_borough mapping:
  Manhattan  = 1
  Bronx      = 2
  Brooklyn   = 3
  Queens     = 4
  Staten Island = 5
"""
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

ACRIS_MASTER_ENDPOINT = "https://data.cityofnewyork.us/resource/bnx9-e6tj.json"
ACRIS_LEGALS_ENDPOINT = "https://data.cityofnewyork.us/resource/8h5j-fqxa.json"
REQUEST_TIMEOUT = 15  # seconds — ACRIS can be slow
MAX_RECORDS = 500     # cap per API call to avoid hitting Socrata limits

BOROUGH_MAP = {
    "manhattan": "1",
    "bronx": "2",
    "brooklyn": "3",
    "queens": "4",
    "staten island": "5",
    "statenisland": "5",  # forgive common typo
}


def _normalize_borough(borough: str) -> Optional[str]:
    """Return the ACRIS borough code for a human-readable borough name, or None."""
    return BOROUGH_MAP.get(borough.strip().lower())


def _median(values: list[float]) -> Optional[float]:
    """Return median of a list, or None if empty."""
    if not values:
        return None
    return round(statistics.median(values), 2)


def get_market_signals(
    borough: str,
    days_back: int = 90,
    min_sale_price: int = 1_000_000,
) -> str:
    """Fetch recent ACRIS deed sales for a NYC borough and return market comps.

    Queries ACRIS Master for DEED/DEEDO transactions in the given borough over
    the lookback window, filters by minimum sale price, then returns aggregate
    statistics and a sample of recent trades.

    Args:
        borough:        NYC borough name (Manhattan, Bronx, Brooklyn, Queens,
                        Staten Island). Case-insensitive.
        days_back:      How many calendar days back to search. Default 90.
        min_sale_price: Filter out transactions below this dollar amount.
                        Default $1,000,000. Set very high (e.g. 999_999_999)
                        to test empty-result handling.

    Returns:
        JSON string with:
          - borough, days_back, min_sale_price   (echo inputs)
          - sale_count                            (int)
          - median_price                          (float | null)
          - sample_sales                          (list of up to 5 records)
          - market_signal                         ("active" | "slow" | "no_data")
          - source_url                            (citable API link)
          - error / message                       (only on failure)
    """
    # ── Input validation ──────────────────────────────────────────────────────
    borough_code = _normalize_borough(borough)
    if borough_code is None:
        return json.dumps({
            "borough": borough,
            "error": "invalid_borough",
            "message": (
                f"'{borough}' is not a recognized NYC borough. "
                "Use: Manhattan, Bronx, Brooklyn, Queens, Staten Island."
            ),
        })

    if days_back < 1:
        return json.dumps({
            "borough": borough,
            "error": "invalid_days_back",
            "message": "days_back must be a positive integer.",
        })

    # ── Build date filter ─────────────────────────────────────────────────────
    cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.000")  # Socrata format

    # SoQL: filter by borough, doc_type IN ('DEED','DEEDO'), price, date
    soql_where = (
        f"recorded_borough='{borough_code}' "
        f"AND (doc_type='DEED' OR doc_type='DEEDO') "
        f"AND document_amt >= '{min_sale_price}' "
        f"AND recorded_datetime >= '{cutoff_str}'"
    )

    source_url = (
        f"{ACRIS_MASTER_ENDPOINT}"
        f"?$where={soql_where.replace(' ', '%20')}"
        f"&$limit={MAX_RECORDS}"
        f"&$order=recorded_datetime%20DESC"
    )

    # ── Call ACRIS Master ─────────────────────────────────────────────────────
    try:
        master_resp = requests.get(
            ACRIS_MASTER_ENDPOINT,
            params={
                "$where": soql_where,
                "$limit": MAX_RECORDS,
                "$order": "recorded_datetime DESC",
                "$select": "document_id,doc_type,document_amt,recorded_datetime,crfn",
            },
            timeout=REQUEST_TIMEOUT,
        )
        master_resp.raise_for_status()
        master_records = master_resp.json()
    except requests.exceptions.Timeout:
        return json.dumps({
            "borough": borough,
            "error": "timeout",
            "message": f"ACRIS Master API did not respond within {REQUEST_TIMEOUT}s.",
        })
    except requests.exceptions.RequestException as exc:
        return json.dumps({
            "borough": borough,
            "error": "request_failed",
            "message": str(exc),
        })
    except ValueError:
        return json.dumps({
            "borough": borough,
            "error": "invalid_response",
            "message": "ACRIS Master API returned non-JSON.",
        })

    # ── Empty result ──────────────────────────────────────────────────────────
    if not master_records:
        return json.dumps({
            "borough": borough,
            "days_back": days_back,
            "min_sale_price": min_sale_price,
            "sale_count": 0,
            "median_price": None,
            "sample_sales": [],
            "market_signal": "no_data",
            "source_url": source_url,
            "note": (
                f"No DEED/DEEDO transactions found in {borough} "
                f"above ${min_sale_price:,} in the last {days_back} days."
            ),
        })

    # ── Enrich top-5 records with address from ACRIS Legals ──────────────────
    # Grab doc_ids for the first 5 records only (avoid a huge IN clause)
    top5_doc_ids = [r["document_id"] for r in master_records[:5]]
    in_clause = ",".join(f"'{d}'" for d in top5_doc_ids)
    address_map: dict[str, str] = {}

    try:
        legals_resp = requests.get(
            ACRIS_LEGALS_ENDPOINT,
            params={
                "$where": f"document_id in ({in_clause})",
                "$select": "document_id,street_number,street_name,borough",
                "$limit": 10,
            },
            timeout=REQUEST_TIMEOUT,
        )
        legals_resp.raise_for_status()
        for leg in legals_resp.json():
            doc_id = leg.get("document_id", "")
            street_no = leg.get("street_number", "")
            street_name = leg.get("street_name", "")
            if doc_id and (street_no or street_name):
                address_map[doc_id] = f"{street_no} {street_name}".strip()
    except Exception:
        # Address enrichment is best-effort — never let it crash the tool
        pass

    # ── Aggregate ─────────────────────────────────────────────────────────────
    prices: list[float] = []
    sample_sales: list[dict] = []

    for rec in master_records:
        try:
            amt = float(rec.get("document_amt", 0))
        except (ValueError, TypeError):
            amt = 0.0
        if amt > 0:
            prices.append(amt)

        if len(sample_sales) < 5:
            doc_id = rec.get("document_id", "")
            sample_sales.append({
                "document_id": doc_id,
                "doc_type": rec.get("doc_type", ""),
                "sale_price": amt,
                "recorded_date": (rec.get("recorded_datetime") or "")[:10],
                "address": address_map.get(doc_id, ""),
                "acris_url": f"https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentDetail?doc_id={doc_id}",
            })

    sale_count = len(master_records)
    median_price = _median(prices)

    # market_signal heuristic — defensible for demo, clearly labeled as such
    if sale_count == 0:
        market_signal = "no_data"
    elif sale_count >= 10:
        market_signal = "active"
    else:
        market_signal = "slow"

    return json.dumps({
        "borough": borough,
        "days_back": days_back,
        "min_sale_price": min_sale_price,
        "sale_count": sale_count,
        "median_price": median_price,
        "sample_sales": sample_sales,
        "market_signal": market_signal,
        "source_url": source_url,
    }, indent=2)


# ── Standalone smoke tests ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("Test 1: Manhattan — real data expected")
    print("=" * 60)
    print(get_market_signals("Manhattan", days_back=90, min_sale_price=1_000_000))

    print("\n" + "=" * 60)
    print("Test 2: Brooklyn — real data expected")
    print("=" * 60)
    print(get_market_signals("Brooklyn", days_back=90, min_sale_price=1_000_000))

    print("\n" + "=" * 60)
    print("Test 3: bad borough — should return error envelope, not crash")
    print("=" * 60)
    print(get_market_signals("Narnia"))

    print("\n" + "=" * 60)
    print("Test 4: absurd min price — should return market_signal: no_data, not crash")
    print("=" * 60)
    print(get_market_signals("Manhattan", days_back=90, min_sale_price=999_999_999))
