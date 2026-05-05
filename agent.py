"""
CRE Deal Pulse — agent loop.

Python orchestrates the four tools (HPD violations, ACRIS market comps,
FRED rates, deterministic underwriting math) so the data is real, fast,
and reproducible. The LLM only scores materiality and writes the briefing
from the structured tool outputs it sees.

The math runs in code, not the model. The model reads the structured
findings, scores 1-5, narrates impact, and pauses on severity-5 for a
human checkpoint before the final markdown briefing.
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
from typing import Any, Callable, Optional

# A run-event callback: fn(kind, payload) -> None. The CLI prints; a future
# API layer (see docs/superpowers/specs/2026-05-01-deal-pulse-ui-design.md)
# will pump these into an SSE stream. Decouples agent core from how events
# are reported.
RunEvent = Callable[[str, dict], None]

from dotenv import load_dotenv
from strands import Agent
from strands.models.litellm import LiteLLMModel

from tools.violations import get_property_distress_signals as _violations_impl
from tools.leasing_signals import get_leasing_signals as _leasing_signals_impl
from tools.market_signals import get_market_signals as _market_signals_impl
from tools.macro_signals import get_macro_signals as _macro_signals_impl
from tools.underwriting import compute_underwriting_delta as _underwriting_impl

# Silence LiteLLM and friends — Hy3 leaks reasoning unless we tell it not to,
# and LiteLLM's debug spam clutters the demo terminal.
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
DEFAULT_LEASE_COMPS_PATH = REPO_ROOT / "data" / "lease_comps_sample.csv"
RUNS_DIR = REPO_ROOT / "runs"

BOROUGH_CODE_TO_NAME = {
    "1": "Manhattan",
    "2": "Bronx",
    "3": "Brooklyn",
    "4": "Queens",
    "5": "Staten Island",
}


# ─────────────────────────────────────────────────────────────────────────────
# Deal loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_deal(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"Deal profile not found at {path}")
    with open(path) as f:
        return json.load(f)


def _resolve_path(path: Optional[Path | str]) -> Optional[Path]:
    if path is None:
        return None
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved
    return REPO_ROOT / resolved


# ─────────────────────────────────────────────────────────────────────────────
# Tool orchestration — Python drives this so the model can't hallucinate the
# tool outputs. Each step prints a single visible status line for the demo.
# ─────────────────────────────────────────────────────────────────────────────

def _print_event(kind: str, payload: dict) -> None:
    """Default RunEvent for the CLI: prints one status line per tool start."""
    if kind == "tool_started":
        print(f"  [tool] {payload['label']} …", flush=True)


def _gather_signals(
    deal: dict,
    observed_rent_override: Optional[float],
    lease_comps_path: Optional[Path],
    lease_days_back: int,
    on_event: RunEvent,
) -> dict:
    prop = deal["property"]
    borough_name = BOROUGH_CODE_TO_NAME.get(prop["borough"], "Manhattan")

    on_event("tool_started", {"tool": "hpd", "label": "HPD violations on target BBL"})
    hpd = json.loads(_violations_impl(prop["bbl"]))

    on_event("tool_started", {"tool": "leasing", "label": "Lease comps rent signal"})
    leasing = json.loads(_leasing_signals_impl(deal, str(lease_comps_path), days_back=lease_days_back))

    on_event("tool_started", {"tool": "acris", "label": f"ACRIS recent sales — {borough_name}"})
    acris = json.loads(_market_signals_impl(borough_name, days_back=90, min_sale_price=1_000_000))

    on_event("tool_started", {"tool": "fred", "label": "FRED 10Y Treasury + SOFR"})
    fred = json.loads(_macro_signals_impl(days_back=30))

    # Choose observed inputs the underwriting tool will price.
    # Rate move comes from FRED treasury_10y (real signal).
    # Rent override wins for demos. Otherwise use the CSV-backed lease signal.
    observed_rate_bps = None
    if isinstance(fred.get("treasury_10y"), dict):
        observed_rate_bps = fred["treasury_10y"].get("bps_change")

    observed_rent_psf = observed_rent_override
    observed_rent_source = "manual_override" if observed_rent_override is not None else None
    if observed_rent_psf is None:
        observed_rent_psf = leasing.get("observed_rent_psf")
        observed_rent_source = leasing.get("source_url")

    on_event("tool_started", {"tool": "underwriting", "label": "Underwriting delta (deterministic Python)"})
    underwriting = json.loads(_underwriting_impl(
        deal,
        observed_rent_psf=observed_rent_psf,
        observed_cap=None,
        observed_rate_bps=observed_rate_bps,
    ))

    return {
        "hpd": hpd,
        "leasing": leasing,
        "acris": acris,
        "fred": fred,
        "underwriting": underwriting,
        "observed_inputs_used": {
            "observed_rent_psf": observed_rent_psf,
            "observed_rent_source": observed_rent_source,
            "observed_rate_bps": observed_rate_bps,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM phases — scoring, then briefing. The model never calls tools directly;
# it only sees the structured outputs Python collected.
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are CRE Deal Pulse, an analyst tool for a NYC commercial \
real estate acquisitions analyst.

You are given a deal profile, the analyst's locked underwriting assumptions, \
and the latest readings from five tools: HPD violations on the target BBL, \
lease comps for market rent, ACRIS recent sales for the submarket, \
FRED 10Y Treasury + SOFR, and a \
deterministic underwriting math tool that computes the dollar and IRR impact \
of any divergence between observed reality and the analyst's assumptions.

You do not call tools. You read the tool outputs that are given to you and \
write a clear, defensible analyst briefing.

You do not do arithmetic in prose. The compute_underwriting_delta tool \
already calculated NOI and IRR deltas — quote those numbers verbatim.

Materiality scoring rules — score each signal 1-5 from the COMPUTED IMPACT, \
not from the raw signal:
  1 = noise, ignore
  2 = minor, monitor
  3 = notable, mention
  4 = significant, flag prominently
  5 = could kill the deal — must be confirmed by the analyst before escalation

Always cite the exact source URL the tool returned. If a tool returned an \
error envelope, mark that signal as 'unavailable' rather than inventing data.

When asked for JSON, emit ONLY JSON — no prose, no markdown fences, no \
preamble. When asked for a briefing, emit clean markdown only."""


def _signals_block(deal: dict, signals: dict) -> str:
    """Render the tool outputs into a compact structured block the LLM will read."""
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

TOOL OUTPUT — Lease comps rent signal
{json.dumps({
    "rent_signal": signals["leasing"].get("rent_signal"),
    "observed_rent_psf": signals["leasing"].get("observed_rent_psf"),
    "comp_count": signals["leasing"].get("comp_count"),
    "median_rent_psf": signals["leasing"].get("median_rent_psf"),
    "weighted_average_rent_psf": signals["leasing"].get("weighted_average_rent_psf"),
    "method": signals["leasing"].get("method"),
    "sample_comps": signals["leasing"].get("sample_comps", [])[:3],
    "filters": signals["leasing"].get("filters"),
    "source_url": signals["leasing"].get("source_url"),
    "error": signals["leasing"].get("error"),
}, indent=2)}

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

TOOL OUTPUT — Deterministic underwriting math
{json.dumps(signals["underwriting"], indent=2)}"""


def _phase1_query(deal: dict, signals: dict) -> str:
    return f"""{_signals_block(deal, signals)}

TASK
Score each meaningful signal 1-5 based on its computed impact on this deal.
Group by category: property, market, macro, underwriting.

You MUST emit a single JSON object with no prose around it, in exactly this \
shape:

{{
  "scoring": [
    {{
      "signal": "<short label, e.g. 'Market rent drift'>",
      "category": "property|market|macro|underwriting",
      "severity": <integer 1-5>,
      "reasoning": "<one sentence citing the computed delta or raw value>",
      "observed": "<observed value with units, or 'unavailable'>",
      "assumed": "<assumed value with units, or 'n/a'>",
      "noi_impact_dollars": <signed integer or null>,
      "irr_impact_pct": <signed decimal e.g. -0.029, or null>,
      "source_url": "<verbatim source URL from the tool output above>",
      "recommendation": "<one short next step for the analyst>"
    }}
  ]
}}

Constraints:
- Use ONLY numbers and URLs that appear in the tool outputs above. Do not invent.
- For NOI and IRR impacts, copy the exact values from \
'TOOL OUTPUT — Deterministic underwriting math' (noi_delta_dollars, irr_delta_pct).
- For the underwriting category signal, the severity MUST equal the \
severity_hint field in the underwriting tool output (it is computed \
deterministically from the IRR magnitude and deal stage).
- For the underwriting signal, source_url MUST be the underwriting tool's \
key driver. Use the lease comps source_url if observed_inputs_used includes \
observed_rent_psf. Use the FRED 10Y source URL if observed_rate_bps is the \
main driver and observed_rent_psf is null.
- If a tool returned an error envelope, score that category 1 and mark \
observed 'unavailable'.
- Output JSON only. No backticks, no markdown, no commentary."""


def _phase2_query(deal: dict, signals: dict, scoring: dict, decision: str) -> str:
    return f"""{_signals_block(deal, signals)}

PRIOR SCORING
{json.dumps(scoring, indent=2)}

ANALYST CHECKPOINT DECISION: {decision}

TASK
Produce the final analyst briefing in clean markdown using exactly this structure:

# CRE Deal Pulse — {deal["deal_id"]}
**Property:** {deal["property"]["address"]}
**As of:** {datetime.now().strftime("%B %d, %Y")}

## Top Alerts (severity 3+)

For each signal scored 3 or higher, write a tight block:
- **Observed:** <observed value>
- **Assumed:** <assumed value>
- **Impact:** <NOI delta $/yr>, IRR <signed pp>  (use 'n/a' if null)
- **Source:** [link text](<source_url>)
- **Recommendation:** <one short action>

Lead with the highest-severity alert. For any severity-5 alert that the \
analyst CONFIRMED, use the killer-quote phrasing form: \
"<Signal description>. Your deal assumes <X>. <Computed dollar impact>; \
IRR drops from <A>% to <B>%. <Recommendation>."

## Full Change Log

One line per signal (including 1s and 2s), grouped by category:
- Property: ...
- Market: ...
- Macro: ...
- Underwriting: ...

If the analyst REJECTED a finding, note it with "(analyst override: not material)".

Constraints:
- Use ONLY numbers and URLs from the tool outputs above. No invented data.
- Quote NOI delta and IRR delta exactly as they appear in the underwriting tool output.
- Output ONLY the markdown briefing — no commentary, no preamble."""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object from a model response, tolerating fences and noise."""
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


def _human_checkpoint(scoring: dict, deal: dict, auto_confirm: bool) -> tuple[str, str]:
    """Print high-severity findings, prompt y/n on stdin (unless --yes), log it.

    Returns (decision_label, raw_user_input).
    """
    high = [s for s in scoring.get("scoring", []) if isinstance(s.get("severity"), int) and s["severity"] >= 5]
    if not high:
        return "auto-approved (no severity 5)", ""

    print("\n" + "=" * 60)
    print("CHECKPOINT — severity-5 finding(s) detected.")
    print("Briefing is paused until you confirm. Reasoning:")
    print("=" * 60)
    for s in high:
        print(f"\n  • {s.get('signal', 'unknown signal')}  [{s.get('category', '?')}]")
        print(f"    Reasoning: {s.get('reasoning', '')}")
        print(f"    Observed:  {s.get('observed', '')}")
        print(f"    Assumed:   {s.get('assumed', '')}")
        if s.get("noi_impact_dollars") is not None:
            print(f"    NOI impact:  ${s['noi_impact_dollars']:,}/yr")
        if s.get("irr_impact_pct") is not None:
            print(f"    IRR impact:  {s['irr_impact_pct'] * 100:+.2f} pts")
        print(f"    Source:    {s.get('source_url', '')}")

    print("\n" + "=" * 60)
    if auto_confirm:
        print("--yes flag set; auto-confirming.")
        answer = "y"
    else:
        answer = input("Approve and finalize the briefing? [y/n]: ").strip().lower()
    decision = "confirmed" if answer in ("y", "yes") else "rejected"

    RUNS_DIR.mkdir(exist_ok=True)
    log_path = RUNS_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    with open(log_path, "w") as f:
        json.dump({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "deal_id": deal.get("deal_id"),
            "decision": decision,
            "raw_input": answer,
            "high_severity_findings": high,
        }, f, indent=2)
    print(f"Decision logged to {log_path.relative_to(REPO_ROOT)}")
    return decision, answer


def _write_run_artifacts(deal: dict, signals: dict, scoring: dict, briefing: str) -> Path:
    """Persist the full run output so terminal output is not the only artifact."""
    RUNS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = RUNS_DIR / f"{timestamp}-{deal.get('deal_id', 'deal')}"
    md_path = base.with_suffix(".md")
    json_path = base.with_suffix(".json")

    with open(md_path, "w") as f:
        f.write(briefing.rstrip() + "\n")
    with open(json_path, "w") as f:
        json.dump({
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "deal_id": deal.get("deal_id"),
            "signals": signals,
            "scoring": scoring,
            "briefing_path": str(md_path.relative_to(REPO_ROOT)),
        }, f, indent=2)

    return md_path


# ─────────────────────────────────────────────────────────────────────────────
# Agent factory + entry point
# ─────────────────────────────────────────────────────────────────────────────

def _build_agent() -> Agent:
    model = LiteLLMModel(
        model_id="openrouter/tencent/hy3-preview:free",
        params={
            # Hy3 is a reasoning model; reasoning tokens count against the
            # max_tokens budget even when hidden from the output stream.
            # Give it room and cap reasoning so it actually emits an answer.
            "max_tokens": 16384,
            "temperature": 0.2,
            "extra_body": {
                "reasoning": {"exclude": True, "max_tokens": 2048},
            },
        },
    )
    # callback_handler=None silences Strands' default streaming print so the
    # demo terminal only shows our explicit status lines + final briefing.
    return Agent(
        model=model,
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )


def run(
    deal_path: Path,
    observed_rent_override: Optional[float],
    lease_comps_path: Optional[Path],
    lease_days_back: int,
    auto_confirm: bool,
    on_event: RunEvent = _print_event,
) -> None:
    deal = _load_deal(deal_path)
    data_sources = deal.get("data_sources") or {}
    resolved_lease_comps_path = _resolve_path(
        lease_comps_path
        or data_sources.get("lease_comps_csv")
        or DEFAULT_LEASE_COMPS_PATH
    )

    print(f"Deal Pulse: {deal['deal_id']} — {deal['property']['address']}")
    print("Scanning…")
    signals = _gather_signals(
        deal,
        observed_rent_override,
        resolved_lease_comps_path,
        lease_days_back,
        on_event,
    )
    print("Scoring with model…")

    agent = _build_agent()

    raw1 = str(agent(_phase1_query(deal, signals)))
    scoring = _extract_json(raw1)
    if scoring is None or "scoring" not in scoring:
        # Hy3 occasionally wraps the JSON in prose despite the system prompt.
        # One repair-retry uses the existing conversation context and asks
        # for JSON only — cheaper and more reliable than re-running phase 1.
        print("  [retry] phase-1 JSON malformed, requesting repair …", flush=True)
        repair = (
            "Your previous response could not be parsed as JSON. "
            "Re-emit the same scoring object with NO prose, NO markdown fences, "
            "and NO commentary — only the JSON object."
        )
        raw1 = str(agent(repair))
        scoring = _extract_json(raw1)
    if scoring is None or "scoring" not in scoring:
        print("\n[ERROR] Could not parse scoring JSON after retry. Raw model output:\n")
        print(raw1)
        sys.exit(1)

    decision, _ = _human_checkpoint(scoring, deal, auto_confirm)

    if decision == "rejected":
        print("\nBriefing suppressed by analyst override. Logged for audit.")
        return

    print("Drafting briefing…")
    raw2 = str(agent(_phase2_query(deal, signals, scoring, decision)))
    report_path = _write_run_artifacts(deal, signals, scoring, raw2)
    print("\n" + "=" * 60)
    print("DEAL PULSE BRIEFING")
    print("=" * 60 + "\n")
    print(raw2)
    print(f"\nSaved report to {report_path.relative_to(REPO_ROOT)}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CRE Deal Pulse agent")
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
        help="Override observed market rent per SF (otherwise computed from lease comps CSV).",
    )
    parser.add_argument(
        "--lease-comps",
        type=Path,
        default=None,
        help="Path to lease comps CSV. Defaults to deal.data_sources.lease_comps_csv or sample data.",
    )
    parser.add_argument(
        "--lease-days",
        type=int,
        default=90,
        help="Lease comps lookback window in days.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm any severity-5 checkpoint without stdin prompt.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.deal, args.observed_rent, args.lease_comps, args.lease_days, args.yes)
