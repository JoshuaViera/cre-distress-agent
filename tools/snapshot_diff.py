"""
Tool: compare_to_yesterday

Loads today's snapshot and the most-recent prior snapshot for a given
deal_id, then walks the two tool_outputs trees and emits the field-level
diffs that are meaningful for an analyst (numeric values + signal-state
categoricals only). Noisy fields like source URLs, free-text narratives,
and per-run timestamps are skipped.

Generic dict walker — adding a fifth signal tool later does NOT require
rewriting this code; new top-level keys appear automatically as
new_signals on the first day they show up.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


DEFAULT_SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent / "snapshots"

# Categorical fields whose value-change matters even though they're strings.
SIGNAL_STATE_KEYS = {"market_signal", "macro_signal", "distress_score"}

# Fields to skip during the diff walk: URLs, free text, sample lists, per-run
# timestamps, snapshot metadata, and the deal-profile echo fields.
SKIP_KEYS = {
    # URLs
    "source_url",
    "source_url_treasury_10y",
    "source_url_sofr",
    # Free text / narration
    "narrative",
    "message",
    # Lists of representative records — useful in the briefing but noisy
    # day-over-day (sort order, sampling drift).
    "sample_violations",
    "sample_sales",
    "drivers",
    "narrative_inputs",
    # Per-run timestamps that move for irrelevant reasons
    "most_recent_violation",
    "captured_at",
    # Snapshot metadata
    "snapshot_version",
    "deal_id",
    "assumptions_locked_at",
    # Deal-constant identifiers — won't change for the same deal
    "bbl",
    "borough",
    "borough_code",
    # Internal stub flag (legacy, defensive)
    "stub",
}


def _is_diffable_leaf(key: str, value) -> bool:
    """Whether a leaf (non-dict) value should be considered for the diff."""
    if key in SKIP_KEYS:
        return False
    if isinstance(value, bool):  # bool is a subclass of int — exclude
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str) and key in SIGNAL_STATE_KEYS:
        return True
    return False


def _walk_diff(today: dict, yesterday: dict, prefix: str) -> list[dict]:
    """Recursively walk two dicts, returning a list of changed-field entries."""
    out: list[dict] = []
    keys = sorted(set(today.keys()) | set(yesterday.keys()))
    for key in keys:
        if key in SKIP_KEYS:
            continue

        path = f"{prefix}.{key}" if prefix else key
        t_val = today.get(key)
        y_val = yesterday.get(key)

        # Both dicts → recurse
        if isinstance(t_val, dict) and isinstance(y_val, dict):
            out.extend(_walk_diff(t_val, y_val, path))
            continue

        # Shape mismatch (one dict, one not) — skip; new_signals/dropped_signals
        # at the top level handle structural changes.
        if isinstance(t_val, dict) or isinstance(y_val, dict):
            continue

        # Only consider leaves that are diffable on at least one side
        if not _is_diffable_leaf(key, t_val) and not _is_diffable_leaf(key, y_val):
            continue

        if t_val == y_val:
            continue

        delta = None
        if (
            isinstance(t_val, (int, float)) and not isinstance(t_val, bool)
            and isinstance(y_val, (int, float)) and not isinstance(y_val, bool)
        ):
            delta = t_val - y_val

        out.append({
            "field": path,
            "yesterday": y_val,
            "today": t_val,
            "delta": delta,
        })
    return out


def _list_snapshot_dates(deal_dir: Path) -> list[str]:
    """Return YYYY-MM-DD dates for all snapshot files in deal_dir, sorted desc."""
    if not deal_dir.is_dir():
        return []
    dates: list[str] = []
    for p in deal_dir.iterdir():
        if p.suffix != ".json":
            continue
        stem = p.stem
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        dates.append(stem)
    dates.sort(reverse=True)
    return dates


def _empty_envelope(deal_id: str, compared_against: Optional[str], reason: Optional[str] = None) -> str:
    payload = {
        "deal_id": deal_id,
        "diff_summary": [],
        "new_signals": [],
        "dropped_signals": [],
        "no_change": True,
        "compared_against": compared_against,
    }
    if reason:
        payload["note"] = reason
    return json.dumps(payload)


def compare_to_yesterday(deal_id: str, snapshots_dir: Optional[Path] = None) -> str:
    """Compare today's snapshot against the most-recent prior snapshot.

    Args:
        deal_id: identifier scoping which snapshot directory to read.
        snapshots_dir: optional override for the snapshots root (used by tests).

    Returns:
        JSON string with: deal_id, diff_summary, new_signals, dropped_signals,
        no_change, compared_against, [optional note].
    """
    base = snapshots_dir or DEFAULT_SNAPSHOTS_DIR
    deal_dir = base / deal_id

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    dates = _list_snapshot_dates(deal_dir)

    prior_dates = [d for d in dates if d != today_str]
    if not prior_dates:
        return _empty_envelope(deal_id, compared_against=None,
                               reason="no prior snapshot — first run for this deal_id")

    prior_date = prior_dates[0]
    prior_path = deal_dir / f"{prior_date}.json"
    today_path = deal_dir / f"{today_str}.json"

    if not today_path.exists():
        return _empty_envelope(deal_id, compared_against=prior_date,
                               reason="today's snapshot not yet written")

    try:
        today_snap = json.loads(today_path.read_text())
        prior_snap = json.loads(prior_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return json.dumps({
            "deal_id": deal_id,
            "error": "snapshot_read_failed",
            "message": str(exc),
            "diff_summary": [],
            "new_signals": [],
            "dropped_signals": [],
            "no_change": True,
            "compared_against": prior_date,
        })

    today_tools = today_snap.get("tool_outputs", {}) or {}
    prior_tools = prior_snap.get("tool_outputs", {}) or {}

    new_signals = sorted(set(today_tools.keys()) - set(prior_tools.keys()))
    dropped_signals = sorted(set(prior_tools.keys()) - set(today_tools.keys()))
    diff_summary = _walk_diff(today_tools, prior_tools, prefix="tool_outputs")

    no_change = not (diff_summary or new_signals or dropped_signals)

    return json.dumps({
        "deal_id": deal_id,
        "diff_summary": diff_summary,
        "new_signals": new_signals,
        "dropped_signals": dropped_signals,
        "no_change": no_change,
        "compared_against": prior_date,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Standalone tests — run `python tools/snapshot_diff.py`
# Exercises the diff against synthetic snapshots in a throwaway tmpdir.
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deal_id = "test-deal-001"
        deal_dir = root / deal_id
        deal_dir.mkdir(parents=True)

        # No prior snapshot → empty envelope
        out = json.loads(compare_to_yesterday(deal_id, snapshots_dir=root))
        assert out["no_change"] is True
        assert out["diff_summary"] == []
        assert out["compared_against"] is None
        assert "no prior snapshot" in out.get("note", "")
        print("[PASS] no prior snapshot returns empty envelope")

        # Plant a yesterday + today; compute diff
        yesterday_payload = {
            "snapshot_version": "v2",
            "captured_at": "2026-05-04T08:00:00+00:00",
            "deal_id": deal_id,
            "tool_outputs": {
                "violations": {
                    "open_violations_count": 12,
                    "distress_score": "medium",
                    "severity_breakdown": {"A": 4, "B": 5, "C": 3, "I": 0},
                    "source_url": "https://data.cityofnewyork.us/...",  # should be skipped
                    "most_recent_violation": "2026-04-30",  # should be skipped
                },
                "macro_signals": {
                    "macro_signal": "stable",
                    "treasury_10y": {"current_value": 4.30, "bps_change": 5},
                },
                "underwriting": {
                    "noi_delta_dollars": -180000,
                    "irr_delta_pct": -0.030,
                    "severity_hint": 5,
                },
            },
        }
        today_payload = {
            "snapshot_version": "v2",
            "captured_at": "2026-05-05T08:00:00+00:00",
            "deal_id": deal_id,
            "tool_outputs": {
                "violations": {
                    "open_violations_count": 14,  # +2
                    "distress_score": "high",     # categorical change
                    "severity_breakdown": {"A": 4, "B": 5, "C": 5, "I": 0},  # C +2
                    "source_url": "https://data.cityofnewyork.us/...different-url",
                    "most_recent_violation": "2026-05-04",
                },
                "macro_signals": {
                    "macro_signal": "rates_moved",  # categorical change
                    "treasury_10y": {"current_value": 4.55, "bps_change": 25},
                },
                "underwriting": {
                    "noi_delta_dollars": -180000,  # unchanged
                    "irr_delta_pct": -0.032,        # changed slightly
                    "severity_hint": 5,
                },
                "market_signals": {  # NEW tool entirely
                    "market_signal": "active",
                    "sale_count": 11,
                },
            },
        }

        # Today is the function's "today" — write the file under the literal
        # today date so the function picks it up correctly.
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        (deal_dir / f"{today_str}.json").write_text(json.dumps(today_payload))
        (deal_dir / "2026-05-04.json").write_text(json.dumps(yesterday_payload))

        out = json.loads(compare_to_yesterday(deal_id, snapshots_dir=root))
        print(json.dumps(out, indent=2))

        assert out["no_change"] is False
        assert out["compared_against"] == "2026-05-04"
        assert out["new_signals"] == ["market_signals"]
        assert out["dropped_signals"] == []

        diff_fields = {entry["field"]: entry for entry in out["diff_summary"]}
        # Numeric changes
        assert "tool_outputs.violations.open_violations_count" in diff_fields
        assert diff_fields["tool_outputs.violations.open_violations_count"]["delta"] == 2
        assert "tool_outputs.violations.severity_breakdown.C" in diff_fields
        assert diff_fields["tool_outputs.violations.severity_breakdown.C"]["delta"] == 2
        assert "tool_outputs.macro_signals.treasury_10y.current_value" in diff_fields
        assert "tool_outputs.macro_signals.treasury_10y.bps_change" in diff_fields
        assert diff_fields["tool_outputs.macro_signals.treasury_10y.bps_change"]["delta"] == 20
        assert "tool_outputs.underwriting.irr_delta_pct" in diff_fields
        # Categorical changes (delta is None)
        assert "tool_outputs.violations.distress_score" in diff_fields
        assert diff_fields["tool_outputs.violations.distress_score"]["delta"] is None
        assert diff_fields["tool_outputs.violations.distress_score"]["yesterday"] == "medium"
        assert diff_fields["tool_outputs.violations.distress_score"]["today"] == "high"
        assert "tool_outputs.macro_signals.macro_signal" in diff_fields
        # Skipped fields stayed skipped
        assert not any("source_url" in f for f in diff_fields)
        assert not any("most_recent_violation" in f for f in diff_fields)
        # Unchanged numeric stayed out
        assert "tool_outputs.underwriting.noi_delta_dollars" not in diff_fields
        assert "tool_outputs.underwriting.severity_hint" not in diff_fields
        print("\n[PASS] real diff catches numeric, categorical, and structural changes")
        print(f"[PASS] noisy fields (URLs, timestamps, samples) correctly skipped")
        print(f"[PASS] new_signals detected: {out['new_signals']}")

        # Same snapshot today as yesterday → no_change
        (deal_dir / f"{today_str}.json").write_text(json.dumps(yesterday_payload))
        out = json.loads(compare_to_yesterday(deal_id, snapshots_dir=root))
        assert out["no_change"] is True
        assert out["diff_summary"] == []
        assert out["compared_against"] == "2026-05-04"
        print("[PASS] identical snapshots produce no_change=True with prior date echoed")

    print("\nAll tests passed.")
