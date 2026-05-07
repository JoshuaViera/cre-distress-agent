"""
Tool: get_leasing_comps
Promotes the v1 ad-hoc `deal["demo_observations"]["observed_rent_psf"]` lookup
into a named, dataflow-visible tool. v2 architecture needs leasing comps to be
a real node so the diagram has somewhere to put the killer-quote input.

Resolution order (highest priority first):
  1. CLI / caller override (e.g., `--observed-rent 68`)
  2. CSV file at `deals/{deal_id}.comps.csv` with columns
     `lease_date,address,rent_psf,sf,tenant`
     (header row required; missing columns ignored).
     Returns the SF-weighted average rent_psf as the observed value.
  3. `deal.demo_observations.observed_rent_psf` fallback for the staged demo.
  4. Returns `observed_rent_psf: null` with `source: "unavailable"` if nothing
     resolves — the underwriting tool will then skip the rent-driven NOI delta.

Pure Python. No network. Same JSON-envelope contract as the other tools.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEALS_DIR = REPO_ROOT / "deals"


def _load_csv_comps(csv_path: Path) -> tuple[Optional[float], list[dict], Optional[str]]:
    """Read a comps CSV and return (sf_weighted_rent_psf, sample_leases, error).

    SF-weighted because a small lease at a headline rate shouldn't dominate
    the comp set. If the CSV has no SF column or all SFs are missing, falls
    back to a simple mean.
    """
    leases: list[dict] = []
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rent = float(row.get("rent_psf", "") or 0)
                except (ValueError, TypeError):
                    rent = 0.0
                if rent <= 0:
                    continue
                try:
                    sf = float(row.get("sf", "") or 0)
                except (ValueError, TypeError):
                    sf = 0.0
                leases.append({
                    "lease_date": row.get("lease_date", ""),
                    "address": row.get("address", ""),
                    "rent_psf": rent,
                    "sf": sf if sf > 0 else None,
                    "tenant": row.get("tenant", ""),
                })
    except OSError as exc:
        return None, [], f"csv_read_failed: {exc}"

    if not leases:
        return None, [], "csv_empty_or_unparseable"

    weighted_total = sum((l["rent_psf"] * l["sf"]) for l in leases if l["sf"])
    weighted_sf = sum(l["sf"] for l in leases if l["sf"])
    if weighted_sf > 0:
        weighted = round(weighted_total / weighted_sf, 2)
    else:
        # Fallback: simple mean if no SF data present.
        weighted = round(sum(l["rent_psf"] for l in leases) / len(leases), 2)

    return weighted, leases[:5], None


def get_leasing_comps(
    deal: dict,
    override_rent_psf: Optional[float] = None,
    deals_dir: Optional[Path] = None,
) -> str:
    """Resolve observed_rent_psf for the deal's submarket.

    Args:
        deal: parsed deal profile dict (must include 'deal_id').
        override_rent_psf: optional caller-provided override (e.g., --observed-rent CLI flag).
        deals_dir: optional override for the deals directory (used by tests).

    Returns:
        JSON string with:
          - observed_rent_psf  (float | null)
          - source             ("cli_override" | "comps_csv" | "deal.demo_observations" | "unavailable")
          - provenance         (one-line human-readable explanation of where the value came from)
          - sample_leases      (list of up to 5 records when source == "comps_csv", else [])
          - csv_path           (string | null — populated when CSV was found)
          - error / message    (only on failure)
    """
    if not isinstance(deal, dict):
        return json.dumps({
            "observed_rent_psf": None,
            "source": "unavailable",
            "provenance": "leasing_comps received non-dict deal profile",
            "sample_leases": [],
            "error": "invalid_deal_profile",
        })

    deal_id = deal.get("deal_id", "")

    # Priority 1: CLI / caller override.
    if override_rent_psf is not None:
        return json.dumps({
            "observed_rent_psf": float(override_rent_psf),
            "source": "cli_override",
            "provenance": f"caller passed override_rent_psf={float(override_rent_psf)}",
            "sample_leases": [],
            "csv_path": None,
        })

    # Priority 2: CSV file at deals/{deal_id}.comps.csv.
    base_dir = deals_dir or DEFAULT_DEALS_DIR
    csv_path = base_dir / f"{deal_id}.comps.csv"
    if csv_path.exists():
        rent, samples, err = _load_csv_comps(csv_path)
        if rent is not None:
            return json.dumps({
                "observed_rent_psf": rent,
                "source": "comps_csv",
                "provenance": (
                    f"SF-weighted average of {len(samples)} lease(s) from "
                    f"{csv_path.relative_to(REPO_ROOT) if csv_path.is_relative_to(REPO_ROOT) else csv_path}"
                ),
                "sample_leases": samples,
                "csv_path": str(csv_path),
            }, indent=2)
        # CSV present but unreadable — fall through to demo_observations
        # rather than 500-ing, but record the failure for the audit trail.
        csv_error = err

    else:
        csv_error = None

    # Priority 3: deal.demo_observations fallback (v1 staged demo path).
    demo_obs = deal.get("demo_observations") or {}
    demo_val = demo_obs.get("observed_rent_psf")
    if demo_val is not None:
        provenance_parts = ["deal.demo_observations.observed_rent_psf"]
        if demo_obs.get("_simulated_signal"):
            provenance_parts.append(f"({demo_obs['_simulated_signal']})")
        if csv_error:
            provenance_parts.append(f"(csv at {csv_path} failed: {csv_error})")
        return json.dumps({
            "observed_rent_psf": float(demo_val),
            "source": "deal.demo_observations",
            "provenance": " ".join(provenance_parts),
            "sample_leases": [],
            "csv_path": None,
        }, indent=2)

    # Priority 4: nothing resolved.
    return json.dumps({
        "observed_rent_psf": None,
        "source": "unavailable",
        "provenance": (
            f"no override, no CSV at {csv_path}, no demo_observations.observed_rent_psf"
            + (f" (csv error: {csv_error})" if csv_error else "")
        ),
        "sample_leases": [],
        "csv_path": None,
    }, indent=2)


# ─────────────────────────────────────────────────────────
# Standalone tests — run `python tools/leasing_comps.py`
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    print("=" * 60)
    print("TEST 1: CLI override wins over everything")
    print("=" * 60)
    deal = {
        "deal_id": "test-001",
        "demo_observations": {"observed_rent_psf": 68.0},
    }
    out = get_leasing_comps(deal, override_rent_psf=72.5)
    parsed = json.loads(out)
    print(out)
    assert parsed["observed_rent_psf"] == 72.5
    assert parsed["source"] == "cli_override"
    print("[PASS] override wins")

    print("\n" + "=" * 60)
    print("TEST 2: demo_observations fallback when no CSV, no override")
    print("=" * 60)
    out = get_leasing_comps(deal)
    parsed = json.loads(out)
    print(out)
    assert parsed["observed_rent_psf"] == 68.0
    assert parsed["source"] == "deal.demo_observations"
    print("[PASS] demo_observations fallback")

    print("\n" + "=" * 60)
    print("TEST 3: CSV beats demo_observations")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        csv_file = tmp_path / "test-001.comps.csv"
        csv_file.write_text(
            "lease_date,address,rent_psf,sf,tenant\n"
            "2026-04-26,100 Main St,66,5000,Tenant A\n"
            "2026-04-28,200 Main St,69,10000,Tenant B\n"
            "2026-04-30,300 Main St,68,5000,Tenant C\n"
        )
        out = get_leasing_comps(deal, deals_dir=tmp_path)
        parsed = json.loads(out)
        print(out)
        assert parsed["source"] == "comps_csv"
        # SF-weighted: (66*5000 + 69*10000 + 68*5000) / 20000 = 1360000/20000 = 68.0
        assert abs(parsed["observed_rent_psf"] - 68.0) < 0.01, parsed["observed_rent_psf"]
        assert len(parsed["sample_leases"]) == 3
        print(f"[PASS] CSV SF-weighted average = {parsed['observed_rent_psf']}")

    print("\n" + "=" * 60)
    print("TEST 4: Empty deal -> unavailable, no crash")
    print("=" * 60)
    out = get_leasing_comps({"deal_id": "no-data-deal"})
    parsed = json.loads(out)
    print(out)
    assert parsed["observed_rent_psf"] is None
    assert parsed["source"] == "unavailable"
    print("[PASS] empty deal -> unavailable")

    print("\n" + "=" * 60)
    print("TEST 5: Bad input -> error envelope, no crash")
    print("=" * 60)
    out = get_leasing_comps("not a dict")  # type: ignore[arg-type]
    parsed = json.loads(out)
    print(out)
    assert parsed["source"] == "unavailable"
    assert parsed.get("error") == "invalid_deal_profile"
    print("[PASS] bad input -> error envelope")

    print("\n" + "=" * 60)
    print("TEST 6: Malformed CSV falls back to demo_observations")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        csv_file = tmp_path / "test-001.comps.csv"
        csv_file.write_text("garbage,that,is,not,a,real,csv\n")
        out = get_leasing_comps(deal, deals_dir=tmp_path)
        parsed = json.loads(out)
        print(out)
        # Falls back to demo_observations because CSV had no usable rows
        assert parsed["source"] == "deal.demo_observations"
        assert "csv" in parsed["provenance"].lower()
        print("[PASS] malformed CSV falls back to demo_observations with note")

    print("\n" + "=" * 60)
    print("TEST 7: Real demo deal — sanity check against the staged scenario")
    print("=" * 60)
    deal_path = REPO_ROOT / "deals" / "midtown-south-office-001.json"
    if deal_path.exists():
        with open(deal_path) as f:
            real_deal = json.load(f)
        out = get_leasing_comps(real_deal)
        parsed = json.loads(out)
        print(out)
        assert parsed["observed_rent_psf"] == 68.0
        assert parsed["source"] == "deal.demo_observations"
        print(f"[PASS] real demo deal -> {parsed['observed_rent_psf']} from {parsed['source']}")
    else:
        print("[SKIP] demo deal file not found at expected path")

    print("\nAll leasing_comps tests passed.")
