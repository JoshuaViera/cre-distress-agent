"""Integration-ish tests for agent.run() — the v2 control flow.

All external calls (HPD, ACRIS, FRED) are mocked via the `fake_requests`
fixture, and the LLM is stubbed via `_StubAgent`. Tests exercise the
band-routing and checkpoint-resume paths without burning any quota.
"""
import builtins
import json
from pathlib import Path

import pytest

import agent


def _register_all_apis_for_demo(fake_requests):
    """Register stub responses sufficient for a clean signals gather."""
    # HPD: zero violations
    fake_requests.register("data.cityofnewyork.us/resource/wvxf-dwi5", [])
    # ACRIS Master: empty (no recent sales)
    fake_requests.register("data.cityofnewyork.us/resource/bnx9-e6tj", [])
    # ACRIS Legals: not reached when master is empty, but register defensively
    fake_requests.register("data.cityofnewyork.us/resource/8h5j-fqxa", [])
    # FRED: stable rates
    fake_requests.register(
        "api.stlouisfed.org",
        {"observations": [
            {"date": "2026-05-05", "value": "4.30"},
            {"date": "2026-04-05", "value": "4.28"},
        ]},
    )


def _stub_dispatch(monkeypatch, band: str):
    """Force dispatch to return a specific band, regardless of underwriting math."""
    edge_map = {
        "green": "auto_render",
        "yellow": "advisory",
        "red": "requires_human_review",
    }

    def _fake(_underwriting):
        return json.dumps({
            "band": band,
            "edge_label": edge_map[band],
            "irr_observed_pct": {"green": 0.12, "yellow": 0.085, "red": 0.05}[band],
            "irr_assumed_pct": 0.14,
            "irr_delta_pct": -0.03,
            "thresholds": {"green_above": 0.10, "red_below": 0.075},
            "dispatch_reason": f"forced {band} for test",
        })

    monkeypatch.setattr(agent, "_dispatch_impl", _fake)


@pytest.fixture
def deal_path():
    return Path(__file__).resolve().parent.parent / "deals" / "midtown-south-office-001.json"


def test_green_band_skips_checkpoint(monkeypatch, fake_requests, tmp_runs_and_snapshots,
                                     stub_agent_class, deal_path, capsys):
    _register_all_apis_for_demo(fake_requests)
    _stub_dispatch(monkeypatch, "green")
    monkeypatch.setenv("FRED_API_KEY", "stub")

    # If checkpoint were called, this would block — make input() raise loudly
    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("checkpoint should not run on green"))

    agent.run(deal_path, observed_rent_override=None, auto_confirm=False)

    # Green path: no checkpoint log, two LLM calls (synthesis + compound_finding)
    assert len(list(tmp_runs_and_snapshots["runs"].iterdir())) == 0
    assert len(stub_agent_class.last_prompts) == 2
    out = capsys.readouterr().out
    assert "DEAL PULSE BRIEFING" in out


def test_yellow_band_skips_checkpoint(monkeypatch, fake_requests, tmp_runs_and_snapshots,
                                      stub_agent_class, deal_path, capsys):
    _register_all_apis_for_demo(fake_requests)
    _stub_dispatch(monkeypatch, "yellow")
    monkeypatch.setenv("FRED_API_KEY", "stub")

    monkeypatch.setattr(builtins, "input", lambda _: pytest.fail("checkpoint should not run on yellow"))

    agent.run(deal_path, observed_rent_override=None, auto_confirm=False)

    assert len(list(tmp_runs_and_snapshots["runs"].iterdir())) == 0
    assert len(stub_agent_class.last_prompts) == 2


def test_red_band_triggers_checkpoint_confirmed(monkeypatch, fake_requests,
                                                tmp_runs_and_snapshots, stub_agent_class,
                                                deal_path, capsys):
    _register_all_apis_for_demo(fake_requests)
    _stub_dispatch(monkeypatch, "red")
    monkeypatch.setenv("FRED_API_KEY", "stub")

    agent.run(deal_path, observed_rent_override=None, auto_confirm=True)

    log_files = list(tmp_runs_and_snapshots["runs"].iterdir())
    assert len(log_files) == 1
    log = json.loads(log_files[0].read_text())
    assert log["decision"] == "confirmed"
    assert log["dispatch_band"] == "red"
    # Both LLM turns ran after confirmation
    assert len(stub_agent_class.last_prompts) == 2
    # Synthesis prompt should reflect the requires_human_review tone branch
    synth_prompt = stub_agent_class.last_prompts[0]
    assert "requires_human_review" in synth_prompt
    assert "killer-quote" in synth_prompt


def test_red_band_downgrade_routes_through_synthesis(monkeypatch, fake_requests,
                                                     tmp_runs_and_snapshots,
                                                     stub_agent_class, deal_path, capsys):
    _register_all_apis_for_demo(fake_requests)
    _stub_dispatch(monkeypatch, "red")
    monkeypatch.setenv("FRED_API_KEY", "stub")

    monkeypatch.setattr(builtins, "input", lambda _: "d")

    agent.run(deal_path, observed_rent_override=None, auto_confirm=False)

    log_files = list(tmp_runs_and_snapshots["runs"].iterdir())
    log = json.loads(log_files[0].read_text())
    assert log["decision"] == "downgrade"
    # Synthesis still runs — but with advisory tone, not requires_human_review
    assert len(stub_agent_class.last_prompts) == 2
    synth_prompt = stub_agent_class.last_prompts[0]
    assert "advisory" in synth_prompt
    assert "analyst downgraded red -> yellow" in synth_prompt


def test_red_band_abort_exits_without_briefing(monkeypatch, fake_requests,
                                               tmp_runs_and_snapshots,
                                               stub_agent_class, deal_path, capsys):
    _register_all_apis_for_demo(fake_requests)
    _stub_dispatch(monkeypatch, "red")
    monkeypatch.setenv("FRED_API_KEY", "stub")

    monkeypatch.setattr(builtins, "input", lambda _: "q")

    agent.run(deal_path, observed_rent_override=None, auto_confirm=False)

    log_files = list(tmp_runs_and_snapshots["runs"].iterdir())
    log = json.loads(log_files[0].read_text())
    assert log["decision"] == "abort"
    # Abort: NO LLM turns should have run
    assert len(stub_agent_class.last_prompts) == 0
    out = capsys.readouterr().out
    assert "Analyst chose abort" in out
    assert "DEAL PULSE BRIEFING" not in out


def test_snapshot_includes_dispatch(monkeypatch, fake_requests, tmp_runs_and_snapshots,
                                    stub_agent_class, deal_path):
    _register_all_apis_for_demo(fake_requests)
    _stub_dispatch(monkeypatch, "yellow")
    monkeypatch.setenv("FRED_API_KEY", "stub")

    agent.run(deal_path, observed_rent_override=None, auto_confirm=True)

    deal_id = json.loads(deal_path.read_text())["deal_id"]
    snap_dir = tmp_runs_and_snapshots["snapshots"] / deal_id
    snap_files = list(snap_dir.iterdir())
    assert len(snap_files) == 1
    snap = json.loads(snap_files[0].read_text())
    assert "dispatch" in snap["tool_outputs"]
    assert snap["tool_outputs"]["dispatch"]["band"] == "yellow"
    assert "leasing_comps" in snap["tool_outputs"]
