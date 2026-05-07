"""Tests for agent._human_checkpoint state machine + helpers."""
import builtins
import json

import pytest

import agent


# ─── _interpret_checkpoint_input ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("y", "confirmed"),
        ("Y", "confirmed"),
        ("yes", "confirmed"),
        ("YES", "confirmed"),
        ("confirm", "confirmed"),
        ("d", "downgrade"),
        ("D", "downgrade"),
        ("downgrade", "downgrade"),
        ("n", "downgrade"),  # legacy alias per spec wording
        ("no", "downgrade"),
        ("q", "abort"),
        ("Q", "abort"),
        ("quit", "abort"),
        ("abort", "abort"),
        ("exit", "abort"),
        # Unrecognized → None (caller re-prompts or defaults to abort)
        ("garbage", None),
        ("", None),
        ("maybe", None),
    ],
)
def test_interpret_checkpoint_input(raw, expected):
    assert agent._interpret_checkpoint_input(raw) == expected


# ─── _downgrade_dispatch ─────────────────────────────────────────────────────

def test_downgrade_dispatch_rewrites_band():
    red = {
        "band": "red",
        "edge_label": "requires_human_review",
        "dispatch_reason": "observed IRR 5.00% below 7.5% red threshold",
        "irr_observed_pct": 0.05,
    }
    downgraded = agent._downgrade_dispatch(red)
    assert downgraded["band"] == "yellow"
    assert downgraded["edge_label"] == "advisory"
    assert downgraded["analyst_downgrade"] is True
    assert "analyst-downgraded from red" in downgraded["dispatch_reason"]
    # original reason preserved inside the rewritten one for audit
    assert "5.00%" in downgraded["dispatch_reason"]


def test_downgrade_dispatch_does_not_mutate_original():
    red = {"band": "red", "edge_label": "requires_human_review", "dispatch_reason": "x"}
    agent._downgrade_dispatch(red)
    assert red["band"] == "red"
    assert red["edge_label"] == "requires_human_review"
    assert "analyst_downgrade" not in red


# ─── _human_checkpoint ───────────────────────────────────────────────────────

def _fixtures():
    deal = {"deal_id": "test-001", "property": {"address": "1 Test St"}}
    dispatch = {
        "band": "red",
        "edge_label": "requires_human_review",
        "dispatch_reason": "observed IRR 5.00% below 7.5% red threshold",
    }
    underwriting = {
        "irr_observed_pct": 0.05,
        "irr_assumed_pct": 0.14,
        "irr_delta_pct": -0.09,
        "noi_delta_dollars": -250000,
        "drivers": [
            {"name": "observed_rent_psf", "observed": 60.0, "assumed": 74.0,
             "unit": "$/SF", "source_tool": "comps_csv"},
        ],
    }
    return deal, dispatch, underwriting


def test_auto_confirm_returns_confirmed(tmp_runs_and_snapshots, capsys):
    deal, dispatch, underwriting = _fixtures()
    result = agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=True)
    assert result["decision"] == "confirmed"
    assert result["raw_input"] == "y"
    log_path = tmp_runs_and_snapshots["runs"]
    log_files = list(log_path.iterdir())
    assert len(log_files) == 1
    log = json.loads(log_files[0].read_text())
    assert log["decision"] == "confirmed"
    assert log["dispatch_band"] == "red"
    assert log["irr_observed_pct"] == 0.05
    assert log["noi_delta_dollars"] == -250000


def test_stdin_y_returns_confirmed(monkeypatch, tmp_runs_and_snapshots):
    deal, dispatch, underwriting = _fixtures()
    monkeypatch.setattr(builtins, "input", lambda _: "y")
    result = agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=False)
    assert result["decision"] == "confirmed"


def test_stdin_d_returns_downgrade(monkeypatch, tmp_runs_and_snapshots):
    deal, dispatch, underwriting = _fixtures()
    monkeypatch.setattr(builtins, "input", lambda _: "d")
    result = agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=False)
    assert result["decision"] == "downgrade"


def test_stdin_q_returns_abort(monkeypatch, tmp_runs_and_snapshots):
    deal, dispatch, underwriting = _fixtures()
    monkeypatch.setattr(builtins, "input", lambda _: "q")
    result = agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=False)
    assert result["decision"] == "abort"


def test_stdin_n_legacy_alias_for_downgrade(monkeypatch, tmp_runs_and_snapshots):
    deal, dispatch, underwriting = _fixtures()
    monkeypatch.setattr(builtins, "input", lambda _: "n")
    result = agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=False)
    assert result["decision"] == "downgrade"


def test_repeated_garbage_input_defaults_to_abort(monkeypatch, tmp_runs_and_snapshots):
    deal, dispatch, underwriting = _fixtures()
    monkeypatch.setattr(builtins, "input", lambda _: "garbage")
    result = agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=False)
    assert result["decision"] == "abort"


def test_audit_log_contains_dispatch_and_underwriting(tmp_runs_and_snapshots):
    deal, dispatch, underwriting = _fixtures()
    agent._human_checkpoint(deal, dispatch, underwriting, auto_confirm=True)
    log_files = list(tmp_runs_and_snapshots["runs"].iterdir())
    log = json.loads(log_files[0].read_text())
    # All forensic fields are present
    for key in ("timestamp_utc", "deal_id", "dispatch_band", "dispatch_edge_label",
                "dispatch_reason", "irr_observed_pct", "irr_assumed_pct",
                "irr_delta_pct", "noi_delta_dollars", "drivers", "decision", "raw_input"):
        assert key in log, f"missing audit field {key}"
