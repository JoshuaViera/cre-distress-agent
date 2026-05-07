"""Tests for tools/leasing_comps.py — observed_rent_psf resolver."""
import json

import pytest

from tools.leasing_comps import get_leasing_comps


@pytest.fixture
def deal_with_demo_obs():
    return {
        "deal_id": "test-001",
        "demo_observations": {
            "observed_rent_psf": 68.0,
            "_simulated_signal": "test signal",
        },
    }


def test_cli_override_wins(deal_with_demo_obs):
    out = json.loads(get_leasing_comps(deal_with_demo_obs, override_rent_psf=72.5))
    assert out["observed_rent_psf"] == 72.5
    assert out["source"] == "cli_override"


def test_demo_observations_fallback(deal_with_demo_obs):
    out = json.loads(get_leasing_comps(deal_with_demo_obs))
    assert out["observed_rent_psf"] == 68.0
    assert out["source"] == "deal.demo_observations"
    assert "test signal" in out["provenance"]


def test_csv_beats_demo_observations(tmp_path, deal_with_demo_obs):
    csv_path = tmp_path / "test-001.comps.csv"
    csv_path.write_text(
        "lease_date,address,rent_psf,sf,tenant\n"
        "2026-04-26,100 Main,66,5000,Tenant A\n"
        "2026-04-28,200 Main,69,10000,Tenant B\n"
        "2026-04-30,300 Main,68,5000,Tenant C\n"
    )
    out = json.loads(get_leasing_comps(deal_with_demo_obs, deals_dir=tmp_path))
    assert out["source"] == "comps_csv"
    # SF-weighted: (66*5k + 69*10k + 68*5k) / 20k = 68.0
    assert abs(out["observed_rent_psf"] - 68.0) < 0.01
    assert len(out["sample_leases"]) == 3


def test_csv_simple_mean_when_no_sf(tmp_path, deal_with_demo_obs):
    csv_path = tmp_path / "test-001.comps.csv"
    csv_path.write_text(
        "lease_date,address,rent_psf,tenant\n"
        "2026-04-26,100 Main,60,Tenant A\n"
        "2026-04-28,200 Main,70,Tenant B\n"
    )
    out = json.loads(get_leasing_comps(deal_with_demo_obs, deals_dir=tmp_path))
    assert out["source"] == "comps_csv"
    assert out["observed_rent_psf"] == 65.0  # simple mean fallback


def test_malformed_csv_falls_back_to_demo(tmp_path, deal_with_demo_obs):
    csv_path = tmp_path / "test-001.comps.csv"
    csv_path.write_text("garbage,not,a,csv\n")
    out = json.loads(get_leasing_comps(deal_with_demo_obs, deals_dir=tmp_path))
    assert out["source"] == "deal.demo_observations"
    assert "csv" in out["provenance"].lower()


def test_empty_deal_returns_unavailable():
    out = json.loads(get_leasing_comps({"deal_id": "no-data"}))
    assert out["observed_rent_psf"] is None
    assert out["source"] == "unavailable"


def test_bad_input_returns_error_envelope():
    out = json.loads(get_leasing_comps("not a dict"))  # type: ignore[arg-type]
    assert out["source"] == "unavailable"
    assert out.get("error") == "invalid_deal_profile"


def test_real_demo_deal(demo_deal):
    """Sanity: the staged Midtown South deal still resolves to 68.0 from demo_observations."""
    out = json.loads(get_leasing_comps(demo_deal))
    assert out["observed_rent_psf"] == 68.0
    assert out["source"] == "deal.demo_observations"
