"""
CRE Deal Pulse — agent loop.

Loads a single staged deal profile, runs four tools (HPD violations, ACRIS
market comps, FRED rates, deterministic underwriting math), scores each
signal 1-5 from the computed impact, pauses for human confirmation on any
severity-5 alert, and emits a markdown briefing.

The math runs in code, not the model. The model reads the world, scores
materiality, and writes the briefing.
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
from typing import Any, Optional

from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel

from tools.violations import get_property_distress_signals as _violations_impl
from tools.market_signals import get_market_signals as _market_signals_impl
from tools.macro_signals import get_macro_signals as _macro_signals_impl
from tools.underwriting import compute_underwriting_delta as _underwriting_impl

logging.getLogger("LiteLLM").setLevel(logging.ERROR)
load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DEAL_PATH = REPO_ROOT / "deals" / "midtown-south-office-001.json"
RUNS_DIR = REPO_ROOT / "runs"

BOROUGH_CODE_TO_NAME = {
    "1": "Manhattan",
    "2": "Bronx",
    "3": "Brooklyn",
    "4": "Queens",
    "5": "Staten Island",
}


# ─────────────────────────────────────────────────────────────────────────────
# Deal profile loading. Module-level so the underwriting @tool wrapper can
# read it without forcing the LLM to round-trip the full JSON as an argument.
# ─────────────────────────────────────────────────────────────────────────────

_DEAL_PROFILE: dict[str, Any] = {}


def _load_deal(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"Deal profile not found at {path}")
    with open(path) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# Tool wrappers exposed to the model. Docstrings are the model's contract —
# they describe when to call each tool and what arguments to pass.
# ─────────────────────────────────────────────────────────────────────────────

@tool
def get_property_distress_signals(bbl: str) -> str:
    """Fetch HPD violations for a NYC BBL and return a distress assessment.

    Use this to check the target property's open code violations and recent
    inspection activity. Returns JSON with violation counts, severity
    breakdown, most recent inspection, and a derived distress_score.

    Args:
        bbl: 10-digit NYC Borough-Block-Lot identifier.
    """
    return _violations_impl(bbl)


@tool
def get_market_signals(
    borough: str,
    days_back: int = 90,
    min_sale_price: int = 1_000_000,
) -> str:
    """Fetch recent ACRIS deed sales for a NYC borough and return market comps.

    Use this to gauge market velocity and pull recent comparable sales near
    the target. Returns JSON with sale_count, median_price, sample_sales,
    and a market_signal label.

    Args:
        borough: NYC borough name (Manhattan, Bronx, Brooklyn, Queens,
                 Staten Island). Case-insensitive.
        days_back: How many calendar days back to search. Default 90.
        min_sale_price: Minimum transaction size to include. Default $1M.
    """
    return _market_signals_impl(borough, days_back, min_sale_price)


@tool
def get_macro_signals(days_back: int = 30) -> str:
    """Fetch FRED 10Y Treasury and SOFR; return current vs. prior + bps_change.

    Use this to check the rate environment versus when the deal was
    underwritten. Returns JSON with current/prior values for DGS10 and SOFR
    plus signed bps_change for each (positive = rates rose).

    Args:
        days_back: Calendar days back to compare against. Default 30.
    """
    return _macro_signals_impl(days_back)


@tool
def compute_underwriting_delta(
    observed_rent_psf: Optional[float] = None,
    observed_cap: Optional[float] = None,
    observed_rate_bps: Optional[float] = None,
) -> str:
    """Compute NOI and IRR impact of observed values vs the loaded deal's
    underwriting assumptions. Pure deterministic Python — same inputs always
    produce the same outputs. The deal profile is loaded once at startup; you
    only pass observed values you want to feed in.

    Call this AFTER the three signal tools so you can pass observed values
    inferred from their outputs.

    Args:
        observed_rent_psf: observed market rent per SF (e.g. from comps or
            recent leases). Omit if no signal worth feeding in.
        observed_cap: observed market exit cap rate as a decimal (e.g. 0.062).
            Omit if no signal.
        observed_rate_bps: change in benchmark rate in basis points
            (e.g. +28 means rates rose 28 bps since lock). Pass the signed
            bps_change from get_macro_signals' treasury_10y block.
    """
    return _underwriting_impl(
        _DEAL_PROFILE,
        observed_rent_psf=observed_rent_psf,
        observed_cap=observed_cap,
        observed_rate_bps=observed_rate_bps,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are CRE Deal Pulse, an analyst tool for a NYC commercial \
real estate acquisitions analyst.

You watch a single deal the analyst is already working. Each run, you check \
three buckets of signals (property-level via HPD, market-level via ACRIS, and \
macro-level via FRED), then call a deterministic underwriting tool to compute \
the dollar and IRR impact of any divergence between observed reality and the \
analyst's locked assumptions.

Math is your tool's job. You do not do arithmetic in prose — you call \
compute_underwriting_delta and quote what it returns.

Materiality scoring is your job. Score each signal 1-5:
  1 = noise, ignore
  2 = minor, monitor
  3 = notable, mention
  4 = significant, flag prominently
  5 = could kill the deal — pause for human review before escalating

Score from the COMPUTED IMPACT, not the raw signal. A 25 bps rate move with a \
$50K NOI hit is a 3, not a 5. A $400K NOI hit on a deal under contract is a 5.

Cite sources. Every flagged finding must reference the source URL the tool \
returned (HPD dataset, ACRIS doc link, FRED series page).

When asked to emit JSON, emit ONLY JSON — no prose, no markdown fences, no \
preamble. When asked for the briefing, emit clean markdown."""


def _phase1_query(deal: dict) -> str:
    """Phase 1: run all four tools, return JSON scoring only."""
    prop = deal["property"]
    uw = deal["underwriting"]
    borough_name = BOROUGH_CODE_TO_NAME.get(prop["borough"], "Manhattan")

    return f"""DEAL PROFILE (already loaded into the underwriting tool):

  deal_id:      {deal["deal_id"]}
  address:      {prop["address"]}
  bbl:          {prop["bbl"]}
  borough:      {borough_name} (code {prop["borough"]})
  submarket:    {prop["submarket"]}
  asset_class:  {prop["asset_class"]}
  square_feet:  {prop["square_footage"]:,}
  deal_stage:   {deal["deal_stage"]}

  ASSUMPTIONS:
    market_rent_psf:  ${uw["market_rent_psf"]:.2f}
    in_place_rent:    ${uw["in_place_rent_psf"]:.2f}
    vacancy_rate:     {uw["vacancy_rate"] * 100:.1f}%
    going_in_cap:     {uw["going_in_cap"] * 100:.2f}%
    exit_cap:         {uw["exit_cap"] * 100:.2f}%
    rent_growth:      {uw["rent_growth"] * 100:.2f}%
    hold_years:       {uw["hold_period_years"]}
    NOI:              ${uw["noi"]:,}
    IRR (stated):     {uw["irr"] * 100:.1f}%

STEP 1 — call get_property_distress_signals(bbl="{prop["bbl"]}")
STEP 2 — call get_market_signals(borough="{borough_name}", days_back=90)
STEP 3 — call get_macro_signals(days_back=30)
STEP 4 — call compute_underwriting_delta with observed values inferred from \
the prior tool outputs:
  - observed_rent_psf: pull from market comps if there's a clear signal of \
where market rent is trading. If ACRIS only shows sale prices (not rents) \
and no comp suggests a rent shift, omit this argument.
  - observed_cap:      omit unless you have a defensible observed cap rate.
  - observed_rate_bps: pass treasury_10y.bps_change from get_macro_signals \
(signed: positive = rates rose).

STEP 5 — Respond with EXACTLY this JSON shape and NOTHING else:

{{
  "scoring": [
    {{
      "signal": "<short label, e.g. 'Market rent drift'>",
      "category": "property|market|macro|underwriting",
      "severity": <1-5>,
      "reasoning": "<one sentence on why this score, citing the computed delta>",
      "observed": "<observed value with units>",
      "assumed": "<assumed value with units>",
      "noi_impact_dollars": <signed integer or null>,
      "irr_impact_pct": <signed decimal e.g. -0.029 or null>,
      "source_url": "<source URL from the tool output>",
      "recommendation": "<one short next step for the analyst>"
    }}
  ]
}}

Do not include any text before or after the JSON object."""


def _phase2_query(deal: dict, scoring: dict, decision: str) -> str:
    """Phase 2: produce the markdown briefing after checkpoint."""
    return f"""The analyst made the following call on the high-severity findings: {decision}.

Now produce the final analyst briefing in clean markdown using this exact structure:

# CRE Deal Pulse — {deal["deal_id"]}
**Property:** {deal["property"]["address"]}
**As of:** {datetime.now().strftime("%B %d, %Y")}

## Top Alerts (severity 3+)

For each signal scoring 3 or higher, write a tight 4-line block:
- **Observed:** <observed value>
- **Assumed:** <assumed value>
- **Impact:** $<NOI delta>/yr, IRR <signed pp>
- **Source:** [link text](<source_url>)
- **Recommendation:** <one short action>

Lead with the highest-severity alert. Use the killer-quote phrasing for any \
severity-5 finding that the analyst confirmed: e.g. "Market rent in {deal["property"]["submarket"]} \
dropped to $X/SF this week. Your deal assumes $Y. NOI falls by $Z and IRR \
drops from A% to B%. Recommend re-running underwriting before LOI."

## Full Change Log

One line per signal (including those scored 1 or 2), grouped by category:
- Property: ...
- Market:   ...
- Macro:    ...
- Underwriting: ...

If the analyst rejected a finding (decision contained "rejected"), note it \
in the change log with "(analyst override: not material)".

Use ONLY data from the prior tool outputs. Do not invent numbers. Quote the \
exact dollar and percentage figures from compute_underwriting_delta.

The scoring you previously emitted is included for your reference:
{json.dumps(scoring, indent=2)}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of a model response. Tolerates code fences and
    leading/trailing reasoning text.
    """
    # Strip fences first
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back to first { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _human_checkpoint(scoring: dict, deal: dict) -> tuple[str, str]:
    """Print high-severity findings, prompt y/n on stdin, log the decision.

    Returns (decision_label, raw_user_input). decision_label is one of:
      "auto-approved (no severity 5)", "confirmed", "rejected".
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


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _build_agent() -> Agent:
    model = LiteLLMModel(
        model_id="openrouter/tencent/hy3-preview:free",
        params={"max_tokens": 8192, "temperature": 0.3},
    )
    return Agent(
        model=model,
        tools=[
            get_property_distress_signals,
            get_market_signals,
            get_macro_signals,
            compute_underwriting_delta,
        ],
        system_prompt=SYSTEM_PROMPT,
    )


def run(deal_path: Path) -> None:
    global _DEAL_PROFILE
    _DEAL_PROFILE = _load_deal(deal_path)

    print(f"Loaded deal: {_DEAL_PROFILE['deal_id']} — {_DEAL_PROFILE['property']['address']}")
    print("Running scan…")

    agent = _build_agent()

    # Phase 1: scoring
    raw1 = str(agent(_phase1_query(_DEAL_PROFILE)))
    scoring = _extract_json(raw1)
    if scoring is None or "scoring" not in scoring:
        print("\n[ERROR] Could not parse scoring JSON from phase 1 output.")
        print("Raw model output below:\n")
        print(raw1)
        sys.exit(1)

    # Checkpoint
    decision, _ = _human_checkpoint(scoring, _DEAL_PROFILE)

    if decision == "rejected":
        print("\nBriefing suppressed by analyst override. Logged for audit.")
        return

    # Phase 2: briefing
    raw2 = str(agent(_phase2_query(_DEAL_PROFILE, scoring, decision)))
    print("\n" + "=" * 60)
    print("DEAL PULSE BRIEFING")
    print("=" * 60 + "\n")
    print(raw2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CRE Deal Pulse agent")
    parser.add_argument(
        "--deal",
        type=Path,
        default=Path(os.getenv("DEAL_PROFILE_PATH", str(DEFAULT_DEAL_PATH))),
        help="Path to a deal profile JSON file.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.deal)
