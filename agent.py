"""
CRE Deal Pulse — agent loop (v2 architecture).

v2 control flow:

    gather_signals
        -> POST_MATH_DISPATCH (deterministic band on absolute observed IRR)
            ├─ green / yellow → straight to synthesis (auto_render / advisory)
            └─ red            → human checkpoint
                                    ├─ y (confirmed) → synthesis as red
                                    ├─ d (downgrade) → band → yellow → synthesis
                                    └─ q (abort)     → exit, no briefing
        -> synthesis turn          (Claude, single LLM call, branches on edge_label)
        -> compound_finding turn   (Claude, second LLM call, cross-signal)
        -> final markdown briefing

The model no longer scores severity. The dispatch tool is the load-bearing
router and the only materiality signal. Claude does synthesis and
compound_finding only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from strands import Agent
from strands.models.litellm import LiteLLMModel

from tools.violations import get_property_distress_signals as _violations_impl
from tools.market_signals import get_market_signals as _market_signals_impl
from tools.macro_signals import get_macro_signals as _macro_signals_impl
from tools.underwriting import compute_underwriting_delta as _underwriting_impl
from tools.snapshot_diff import compare_to_yesterday as _snapshot_diff_impl
from tools.dispatch import post_math_dispatch as _dispatch_impl
from tools.leasing_comps import get_leasing_comps as _leasing_comps_impl

# Silence LiteLLM noise (unchanged from v1)
for name in ("LiteLLM", "litellm", "httpx"):
    logging.getLogger(name).setLevel(logging.ERROR)
os.environ.setdefault("LITELLM_LOG", "ERROR")
try:
    import litellm  # type: ignore[import-not-found]
    litellm.suppress_debug_info = True
except Exception:
    pass

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DEAL_PATH = REPO_ROOT / "deals" / "midtown-south-office-001.json"
RUNS_DIR = REPO_ROOT / "runs"
PROMPTS_DIR = REPO_ROOT / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt_v2.txt"
COMPOUND_FINDING_PROMPT_PATH = PROMPTS_DIR / "compound_finding.txt"
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
SNAPSHOT_VERSION = "v2"

BOROUGH_CODE_TO_NAME = {
    "1": "Manhattan",
    "2": "Bronx",
    "3": "Brooklyn",
    "4": "Queens",
    "5": "Staten Island",
}

CheckpointDecision = Literal["confirmed", "downgrade", "abort"]


# ─────────────────────────────────────────────────────────────────────────────
# Deal loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_deal(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"Deal profile not found at {path}")
    with open(path) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Tool orchestration — Python drives this so the model can't hallucinate the
# tool outputs. Each step prints a single visible status line for the demo.
# ─────────────────────────────────────────────────────────────────────────────

def _step(label: str) -> None:
    print(f"  [tool] {label} …", flush=True)


def _safe_rel(path: Path) -> str:
    """Format a path relative to REPO_ROOT for display, falling back to the
    absolute string when the path lives outside the repo (test tmp dirs, etc).
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _write_snapshot(deal_id: str, tool_outputs: dict) -> Path:
    """Atomic-write today's snapshot of the ground-truth tool outputs.

    Filename is the UTC date the run was initiated; same-date runs overwrite.
    Uses os.replace for cross-platform atomic rename. v2 adds dispatch to
    the captured tool_outputs so the diff can detect band transitions.
    """
    snap_dir = SNAPSHOTS_DIR / deal_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    final_path = snap_dir / f"{today}.json"
    tmp_path = snap_dir / f"{today}.json.tmp"
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "deal_id": deal_id,
        "tool_outputs": tool_outputs,
    }
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, final_path)
    return final_path


def _gather_signals(deal: dict, observed_rent_override: Optional[float]) -> dict:
    prop = deal["property"]
    borough_name = BOROUGH_CODE_TO_NAME.get(prop["borough"], "Manhattan")

    _step("HPD violations on target BBL")
    hpd = json.loads(_violations_impl(prop["bbl"]))

    _step(f"ACRIS recent sales — {borough_name}")
    acris = json.loads(_market_signals_impl(borough_name, days_back=90, min_sale_price=1_000_000))

    _step("FRED 10Y Treasury + SOFR")
    fred = json.loads(_macro_signals_impl(days_back=30))

    # Rate move comes from FRED treasury_10y (real signal).
    observed_rate_bps = None
    observed_rate_source = None
    if isinstance(fred.get("treasury_10y"), dict):
        observed_rate_bps = fred["treasury_10y"].get("bps_change")
        if observed_rate_bps is not None:
            observed_rate_source = "macro_signals"

    # Leasing comps (v2): named tool. Reads CSV if present, else demo_observations,
    # else honors a CLI override. Returns provenance + sample_leases.
    _step("Leasing comps (CSV / demo_observations)")
    leasing = json.loads(_leasing_comps_impl(deal, override_rent_psf=observed_rent_override))
    observed_rent_psf = leasing.get("observed_rent_psf")
    observed_rent_psf_source = leasing.get("source")

    _step("Underwriting delta (deterministic Python)")
    underwriting = json.loads(_underwriting_impl(
        deal,
        observed_rent_psf=observed_rent_psf,
        observed_cap=None,
        observed_rate_bps=observed_rate_bps,
        observed_rent_psf_source=observed_rent_psf_source,
        observed_rate_source=observed_rate_source,
    ))

    _step("POST_MATH_DISPATCH (band classifier)")
    dispatch = json.loads(_dispatch_impl(underwriting))
    print(
        f"    band: {dispatch['band']} ({dispatch.get('edge_label', '?')}) — "
        f"{dispatch.get('dispatch_reason', '')}",
        flush=True,
    )

    # Snapshot ground-truth tool outputs (NOT model output). v2 captures
    # dispatch alongside the four signal tools so day-over-day diffs include
    # band transitions (e.g., yellow -> red) as a first-class change vector.
    deal_id = deal["deal_id"]
    tool_outputs_for_snapshot = {
        "violations": hpd,
        "market_signals": acris,
        "macro_signals": fred,
        "leasing_comps": leasing,
        "underwriting": underwriting,
        "dispatch": dispatch,
    }
    _step("Snapshot write")
    snap_path = _write_snapshot(deal_id, tool_outputs_for_snapshot)
    print(f"    wrote {_safe_rel(snap_path)}", flush=True)

    _step("Snapshot diff vs prior snapshot")
    snapshot_diff = json.loads(_snapshot_diff_impl(deal_id, snapshots_dir=SNAPSHOTS_DIR))

    return {
        "hpd": hpd,
        "acris": acris,
        "fred": fred,
        "leasing": leasing,
        "snapshot_diff": snapshot_diff,
        "underwriting": underwriting,
        "dispatch": dispatch,
        "observed_inputs_used": {
            "observed_rent_psf": observed_rent_psf,
            "observed_rent_psf_source": observed_rent_psf_source,
            "observed_rate_bps": observed_rate_bps,
            "observed_rate_source": observed_rate_source,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM phases — synthesis + compound_finding. The model never scores severity.
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
COMPOUND_FINDING_PROMPT = COMPOUND_FINDING_PROMPT_PATH.read_text(encoding="utf-8").strip()


def _render_snapshot_diff(diff: dict) -> str:
    """Render the snapshot diff for the LLM context.

    First runs (no prior snapshot) and unchanged runs render as one-line
    notes so they add minimal context noise. Only when there's actual diff
    content do we hand the model the full JSON.
    """
    if diff.get("no_change"):
        if not diff.get("compared_against"):
            return ("TOOL OUTPUT — Snapshot diff vs prior snapshot\n"
                    "(No prior snapshot for this deal_id; this is the first run. "
                    "Diff context not yet available.)")
        return ("TOOL OUTPUT — Snapshot diff vs prior snapshot\n"
                f"(No material changes vs {diff['compared_against']}.)")
    return ("TOOL OUTPUT — Snapshot diff vs prior snapshot\n"
            + json.dumps(diff, indent=2))


def _signals_block(deal: dict, signals: dict) -> str:
    """Render the tool outputs into a compact structured block the LLM will read."""
    leasing_compact = {
        "observed_rent_psf": signals["leasing"].get("observed_rent_psf"),
        "source": signals["leasing"].get("source"),
        "provenance": signals["leasing"].get("provenance"),
        "sample_leases": signals["leasing"].get("sample_leases", [])[:3],
        "error": signals["leasing"].get("error"),
    }
    return f"""DEAL
{json.dumps({
    "deal_id": deal["deal_id"],
    "property": deal["property"],
    "deal_stage": deal["deal_stage"],
    "underwriting_assumptions": deal["underwriting"],
}, indent=2)}

OBSERVED INPUTS USED BY UNDERWRITING MATH
{json.dumps(signals["observed_inputs_used"], indent=2)}

TOOL OUTPUT — HPD violations
{json.dumps(signals["hpd"], indent=2)}

TOOL OUTPUT — ACRIS recent sales
{json.dumps({
    "borough": signals["acris"].get("borough"),
    "sale_count": signals["acris"].get("sale_count"),
    "median_price": signals["acris"].get("median_price"),
    "market_signal": signals["acris"].get("market_signal"),
    "sample_sales": signals["acris"].get("sample_sales", [])[:3],
    "source_url": signals["acris"].get("source_url"),
    "error": signals["acris"].get("error"),
}, indent=2)}

TOOL OUTPUT — FRED rates
{json.dumps(signals["fred"], indent=2)}

TOOL OUTPUT — Leasing comps
{json.dumps(leasing_compact, indent=2)}

TOOL OUTPUT — Deterministic underwriting math
{json.dumps(signals["underwriting"], indent=2)}

TOOL OUTPUT — POST_MATH_DISPATCH band
{json.dumps(signals["dispatch"], indent=2)}

{_render_snapshot_diff(signals["snapshot_diff"])}"""


_EDGE_TONE = {
    "auto_render": (
        "TONE: deal is GREEN (observed IRR above 10% hurdle). "
        "Brief is informational. Lead with what's working. Note any sub-material "
        "changes from the snapshot diff but do not raise alarm. No checkpoint "
        "was required for this run."
    ),
    "advisory": (
        "TONE: deal is YELLOW (observed IRR in 7.5%-10% watch band). "
        "Brief is a watch-list flag. Lead with the band classification and the "
        "specific drivers (rent, rate, cap) that pushed it into watch. "
        "Recommend the analyst monitor named signals next run."
    ),
    "requires_human_review": (
        "TONE: deal is RED (observed IRR below 7.5%). The analyst has already "
        "reviewed at the checkpoint — render the briefing in killer-quote form. "
        "Lead with the dollar and IRR impact. Use phrasing: "
        "'<Signal>. Your deal assumes <X>. <Computed dollar impact>; IRR drops "
        "from <A>% to <B>%. <Recommendation>.'"
    ),
}


def _synthesis_query(deal: dict, signals: dict, checkpoint_outcome: str) -> str:
    """Single LLM turn: produces the analyst briefing in markdown.

    Branches tone on dispatch.edge_label (auto_render / advisory / requires_human_review).
    Math has already classified materiality — the model only narrates.
    """
    edge = signals["dispatch"].get("edge_label", "advisory")
    tone = _EDGE_TONE.get(edge, _EDGE_TONE["advisory"])
    band = signals["dispatch"].get("band", "yellow")
    return f"""{_signals_block(deal, signals)}

CHECKPOINT OUTCOME: {checkpoint_outcome}

{tone}

TASK
Produce the analyst briefing in clean markdown using exactly this structure:

# CRE Deal Pulse — {deal["deal_id"]}
**Property:** {deal["property"]["address"]}
**As of:** {datetime.now().strftime("%B %d, %Y")}
**Band:** {band.upper()} ({edge})

## Headline
One sentence stating the band and the single biggest driver of it.

## Top Signals
For each signal that is materially driving the band, write a tight block.
Only include signals with a non-trivial computed impact or a notable raw value.
- **Observed:** <observed value>
- **Assumed:** <assumed value>
- **Impact:** <NOI delta $/yr>, IRR <signed pp>  (use 'n/a' if null)
- **Source:** [link text](<source_url>)
- **Recommendation:** <one short action>

## Full Change Log
One line per signal, grouped by category (Property / Market / Macro / Underwriting).
If the snapshot diff shows day-over-day changes, mention them inline.

Constraints:
- Use ONLY numbers and URLs from the tool outputs above. No invented data.
- Quote NOI delta and IRR delta exactly as they appear in the underwriting tool output.
- DO NOT score severity 1-5. The dispatch band is the only materiality signal.
- Output ONLY the markdown briefing — no commentary, no preamble."""


def _compound_finding_query(deal: dict, signals: dict) -> str:
    """Third LLM turn: cross-signal compound findings.

    Reads today's signals + snapshot_diff temporal vectors. Outputs a markdown
    section appended to the synthesis briefing. Hard constraints in the
    prompt prevent speculative single-signal narratives.
    """
    return f"""{_signals_block(deal, signals)}

{COMPOUND_FINDING_PROMPT}"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object from a model response (kept for future structured turns)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _interpret_checkpoint_input(raw: str) -> Optional[CheckpointDecision]:
    """Map raw stdin to a control signal. Returns None on unrecognized input.

    Canonical keys: y (confirm), d (downgrade), q (abort).
    Legacy aliases accepted for back-compat with v1 muscle memory:
      'n' / 'no' map to downgrade (matches the v2 spec wording
      'n -> route back through stage 3 with severity downgraded').
    """
    s = (raw or "").strip().lower()
    if s in ("y", "yes", "confirm", "confirmed"):
        return "confirmed"
    if s in ("d", "downgrade", "n", "no"):
        return "downgrade"
    if s in ("q", "quit", "abort", "exit"):
        return "abort"
    return None


def _human_checkpoint(
    deal: dict,
    dispatch: dict,
    underwriting: dict,
    auto_confirm: bool,
) -> dict:
    """Pause on RED dispatch band, prompt analyst, return control signal.

    Returns a dict with:
      - decision: 'confirmed' | 'downgrade' | 'abort'
      - raw_input: original stdin string (or 'y' if --yes)
      - log_path: path to the audit log file written

    Audit log records the dispatch reason, observed/assumed IRR, NOI delta,
    drivers, and the final decision for forensic replay.
    """
    print("\n" + "=" * 60)
    print(f"CHECKPOINT — dispatch band: RED ({dispatch.get('edge_label', '?')})")
    print("Synthesis is paused until you respond.")
    print("=" * 60)

    print(f"\n  Reason:    {dispatch.get('dispatch_reason', '')}")
    irr_o = underwriting.get("irr_observed_pct")
    irr_a = underwriting.get("irr_assumed_pct")
    irr_d = underwriting.get("irr_delta_pct")
    noi_d = underwriting.get("noi_delta_dollars")
    if irr_a is not None and irr_o is not None:
        line = f"  IRR:       assumed {irr_a * 100:.2f}% -> observed {irr_o * 100:.2f}%"
        if irr_d is not None:
            line += f"  (delta {irr_d * 100:+.2f} pp)"
        print(line)
    if noi_d is not None:
        print(f"  NOI delta: ${noi_d:,}/yr")

    drivers = underwriting.get("drivers") or []
    if drivers:
        print(f"  Drivers ({len(drivers)}):")
        for drv in drivers:
            print(
                f"    - {drv.get('name')}: observed={drv.get('observed')} "
                f"assumed={drv.get('assumed')} unit={drv.get('unit')} "
                f"source={drv.get('source_tool')}"
            )

    print("\n" + "=" * 60)
    print("Choose:")
    print("  [y] confirmed  — proceed to synthesis as red / requires_human_review")
    print("  [d] downgrade  — accept band downgrade to yellow / advisory, then synthesize")
    print("  [q] abort      — log decision, exit without producing a briefing")
    print("=" * 60)

    if auto_confirm:
        print("--yes flag set; auto-confirming.")
        raw = "y"
        decision: Optional[CheckpointDecision] = "confirmed"
    else:
        raw = ""
        decision = None
        for _attempt in range(2):
            raw = input("Decision [y/d/q]: ").strip()
            interpreted = _interpret_checkpoint_input(raw)
            if interpreted is not None:
                decision = interpreted
                break
            print(f"  Unrecognized input '{raw}'. Use y, d, or q.")
        if decision is None:
            print("  No valid response after 2 attempts; defaulting to abort.")
            decision = "abort"

    RUNS_DIR.mkdir(exist_ok=True)
    log_path = RUNS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    with open(log_path, "w") as f:
        json.dump({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "deal_id": deal.get("deal_id"),
            "dispatch_band": dispatch.get("band"),
            "dispatch_edge_label": dispatch.get("edge_label"),
            "dispatch_reason": dispatch.get("dispatch_reason"),
            "irr_observed_pct": irr_o,
            "irr_assumed_pct": irr_a,
            "irr_delta_pct": irr_d,
            "noi_delta_dollars": noi_d,
            "drivers": drivers,
            "decision": decision,
            "raw_input": raw,
        }, f, indent=2)
    print(f"Decision logged to {_safe_rel(log_path)}")

    return {
        "decision": decision,
        "raw_input": raw,
        "log_path": str(log_path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Agent factory + entry point
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MODEL_ID = "openrouter/tencent/hy3-preview:free"


def _model_wants_hy3_reasoning_extra_body(model_id: str) -> bool:
    """LiteLLM forwards `params.extra_body` to the provider. Anthropic's API
    rejects unknown keys with 400 ('extra_body: Extra inputs are not permitted').
    Only Hy3 / Tencent reasoning models need the reasoning budget block.
    """
    mid = model_id.lower()
    return "hy3" in mid or "tencent" in mid


def _build_agent() -> Agent:
    model_id = os.getenv("MODEL_ID", DEFAULT_MODEL_ID)
    params: dict[str, Any] = {
        "max_tokens": 16384,
        "temperature": 0.2,
    }
    if _model_wants_hy3_reasoning_extra_body(model_id):
        params["extra_body"] = {
            "reasoning": {"exclude": True, "max_tokens": 2048},
        }
    model = LiteLLMModel(
        model_id=model_id,
        params=params,
    )
    return Agent(
        model=model,
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )


def _downgrade_dispatch(dispatch: dict) -> dict:
    """Force dispatch band one notch down: red -> yellow.

    Used when the analyst chose 'd' at the checkpoint. Returns a copy;
    the snapshot already on disk stays as-captured for audit.
    """
    downgraded = dict(dispatch)
    downgraded["band"] = "yellow"
    downgraded["edge_label"] = "advisory"
    downgraded["dispatch_reason"] = (
        "analyst-downgraded from red. original reason: "
        f"{dispatch.get('dispatch_reason', '')}"
    )
    downgraded["analyst_downgrade"] = True
    return downgraded


def run(deal_path: Path, observed_rent_override: Optional[float], auto_confirm: bool) -> None:
    deal = _load_deal(deal_path)

    print(f"Deal Pulse: {deal['deal_id']} — {deal['property']['address']}")
    print("Scanning…")
    signals = _gather_signals(deal, observed_rent_override)

    dispatch = signals["dispatch"]
    band = dispatch.get("band", "red")

    checkpoint_outcome = "no checkpoint required (band not red)"
    if band == "red":
        result = _human_checkpoint(deal, dispatch, signals["underwriting"], auto_confirm)
        decision = result["decision"]
        if decision == "abort":
            print("\nAnalyst chose abort. No briefing produced. Decision logged for audit.")
            return
        if decision == "downgrade":
            signals["dispatch"] = _downgrade_dispatch(dispatch)
            checkpoint_outcome = "analyst downgraded red -> yellow"
        else:
            checkpoint_outcome = "analyst confirmed red"

    agent = _build_agent()

    print("Drafting synthesis…")
    raw_synth = str(agent(_synthesis_query(deal, signals, checkpoint_outcome)))

    print("Drafting compound findings…")
    raw_compound = str(agent(_compound_finding_query(deal, signals)))

    print("\n" + "=" * 60)
    print("DEAL PULSE BRIEFING")
    print("=" * 60 + "\n")
    print(raw_synth.strip())
    print("\n")
    print(raw_compound.strip())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CRE Deal Pulse agent (v2)")
    parser.add_argument(
        "--deal",
        type=Path,
        default=Path(os.getenv("DEAL_PROFILE_PATH", str(DEFAULT_DEAL_PATH))),
        help="Path to a deal profile JSON file.",
    )
    parser.add_argument(
        "--observed-rent",
        type=float,
        default=None,
        help="Override observed market rent per SF (otherwise pulled from leasing_comps tool).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm any RED-band checkpoint without stdin prompt.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.deal, args.observed_rent, args.yes)
