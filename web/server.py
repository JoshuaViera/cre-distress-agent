"""
CRE Deal Pulse v2 — Web Server
Flask backend that streams agent tool outputs and Claude synthesis
to the frontend via Server-Sent Events (SSE).

Usage:
    cd web && python server.py
    Then open http://localhost:5050
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add repo root to path so we can import tools/ and agent helpers
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env", override=True)

from flask import Flask, Response, jsonify, request, send_from_directory

from tools.violations import get_property_distress_signals as _violations_impl
from tools.market_signals import get_market_signals as _market_signals_impl
from tools.macro_signals import get_macro_signals as _macro_signals_impl
from tools.underwriting import compute_underwriting_delta as _underwriting_impl
from tools.snapshot_diff import compare_to_yesterday as _snapshot_diff_impl
from tools.dispatch import post_math_dispatch as _dispatch_impl
from tools.leasing_comps import get_leasing_comps as _leasing_comps_impl

app = Flask(__name__, static_folder=str(Path(__file__).parent))

DEALS_DIR      = REPO_ROOT / "deals"
SNAPSHOTS_DIR  = REPO_ROOT / "snapshots"
RUNS_DIR       = REPO_ROOT / "runs"
PROMPTS_DIR    = REPO_ROOT / "prompts"

SYSTEM_PROMPT          = (PROMPTS_DIR / "system_prompt_v2.txt").read_text(encoding="utf-8").strip()
COMPOUND_FINDING_PROMPT = (PROMPTS_DIR / "compound_finding.txt").read_text(encoding="utf-8").strip()

BOROUGH_CODE_TO_NAME = {
    "1": "Manhattan", "2": "Bronx", "3": "Brooklyn",
    "4": "Queens",    "5": "Staten Island",
}

# In-memory run registry  {run_id -> RunState}
_runs: dict[str, "RunState"] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Run state
# ─────────────────────────────────────────────────────────────────────────────

class RunState:
    def __init__(self, run_id: str, deal_id: str):
        self.run_id   = run_id
        self.deal_id  = deal_id
        self.q: queue.Queue = queue.Queue()
        self.checkpoint_q: queue.Queue = queue.Queue()
        self.token_usage = {"input": 0, "output": 0}

    def emit(self, event_type: str, data: dict) -> None:
        self.q.put({"type": event_type, "data": data})

    def close(self) -> None:
        self.q.put(None)


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot helpers (inlined so server.py has no agent.py import)
# ─────────────────────────────────────────────────────────────────────────────

def _write_snapshot(deal_id: str, tool_outputs: dict) -> Path:
    snap_dir = SNAPSHOTS_DIR / deal_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    final_path = snap_dir / f"{today}.json"
    tmp_path   = snap_dir / f"{today}.json.tmp"
    payload = {
        "snapshot_version": "v2",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "deal_id": deal_id,
        "tool_outputs": tool_outputs,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, final_path)
    return final_path


def _render_diff(diff: dict) -> str:
    if diff.get("no_change"):
        if not diff.get("compared_against"):
            return "(No prior snapshot — first run.)"
        return f"(No material changes vs {diff['compared_against']}.)"
    return json.dumps(diff, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# LLM prompt builders (mirrors agent.py logic)
# ─────────────────────────────────────────────────────────────────────────────

_EDGE_TONE = {
    "auto_render": (
        "TONE: deal is GREEN (observed IRR above 10% hurdle). "
        "Brief is informational. Lead with what's working."
    ),
    "advisory": (
        "TONE: deal is YELLOW (observed IRR in 7.5%-10% watch band). "
        "Brief is a watch-list flag. Lead with the band classification and drivers."
    ),
    "requires_human_review": (
        "TONE: deal is RED (observed IRR below 7.5%). Analyst has reviewed. "
        "Render in killer-quote form. Lead with dollar and IRR impact."
    ),
}

def _signals_block(deal: dict, signals: dict) -> str:
    leasing_compact = {
        "observed_rent_psf": signals["leasing"].get("observed_rent_psf"),
        "source":            signals["leasing"].get("source"),
        "provenance":        signals["leasing"].get("provenance"),
        "sample_leases":     signals["leasing"].get("sample_leases", [])[:3],
    }
    return f"""DEAL
{json.dumps({"deal_id": deal["deal_id"], "property": deal["property"],
             "deal_stage": deal["deal_stage"],
             "underwriting_assumptions": deal["underwriting"]}, indent=2)}

OBSERVED INPUTS USED BY UNDERWRITING MATH
{json.dumps(signals["observed_inputs_used"], indent=2)}

TOOL OUTPUT — HPD violations
{json.dumps(signals["hpd"], indent=2)}

TOOL OUTPUT — ACRIS recent sales
{json.dumps({"borough": signals["acris"].get("borough"),
             "sale_count": signals["acris"].get("sale_count"),
             "median_price": signals["acris"].get("median_price"),
             "market_signal": signals["acris"].get("market_signal"),
             "sample_sales": signals["acris"].get("sample_sales", [])[:3]}, indent=2)}

TOOL OUTPUT — FRED rates
{json.dumps(signals["fred"], indent=2)}

TOOL OUTPUT — Leasing comps
{json.dumps(leasing_compact, indent=2)}

TOOL OUTPUT — Deterministic underwriting math
{json.dumps(signals["underwriting"], indent=2)}

TOOL OUTPUT — POST_MATH_DISPATCH band
{json.dumps(signals["dispatch"], indent=2)}

TOOL OUTPUT — Snapshot diff vs prior snapshot
{_render_diff(signals["snapshot_diff"])}"""


def _synthesis_prompt(deal: dict, signals: dict, checkpoint_outcome: str) -> str:
    edge = signals["dispatch"].get("edge_label", "advisory")
    tone = _EDGE_TONE.get(edge, _EDGE_TONE["advisory"])
    band = signals["dispatch"].get("band", "yellow").upper()
    return f"""{_signals_block(deal, signals)}

CHECKPOINT OUTCOME: {checkpoint_outcome}

{tone}

TASK
Produce the analyst briefing in clean markdown:

# CRE Deal Pulse — {deal["deal_id"]}
**Property:** {deal["property"]["address"]}
**As of:** {datetime.now().strftime("%B %d, %Y")}
**Band:** {band} ({edge})

## Headline
One sentence with band and biggest driver.

## Top Signals
For each material signal:
- **Observed:** <value>
- **Assumed:** <value>
- **Impact:** <NOI delta $/yr>, IRR <signed pp>
- **Source:** [link](<url>)
- **Recommendation:** <one action>

## Full Change Log
One line per signal grouped by category. Include day-over-day snapshot changes.

Constraints:
- Use ONLY numbers from tool outputs. No invented data.
- DO NOT score severity 1-5.
- Output ONLY the markdown briefing."""


def _compound_prompt(deal: dict, signals: dict) -> str:
    return f"{_signals_block(deal, signals)}\n\n{COMPOUND_FINDING_PROMPT}"


# ─────────────────────────────────────────────────────────────────────────────
# Agent thread
# ─────────────────────────────────────────────────────────────────────────────

def _run_agent(state: RunState, deal: dict) -> None:
    try:
        prop         = deal["property"]
        borough_name = BOROUGH_CODE_TO_NAME.get(prop["borough"], "Manhattan")

        def tool(name: str, detail: str, fn, *args, **kwargs):
            state.emit("tool_start", {"tool": name, "detail": detail})
            result = json.loads(fn(*args, **kwargs))
            state.emit("tool_done",  {"tool": name, "result": result})
            return result

        hpd  = tool("HPD Violations",     f"BBL {prop['bbl']}",
                    _violations_impl, prop["bbl"])
        acris = tool("ACRIS Sales Comps",  f"{borough_name} — 90 days",
                    _market_signals_impl, borough_name, 90, 1_000_000)
        fred  = tool("FRED Macro Signals", "10Y Treasury + SOFR",
                    _macro_signals_impl, 30)

        observed_rate_bps    = None
        observed_rate_source = None
        if isinstance(fred.get("treasury_10y"), dict):
            observed_rate_bps = fred["treasury_10y"].get("bps_change")
            if observed_rate_bps is not None:
                observed_rate_source = "macro_signals"

        leasing = tool("Leasing Comps", "CSV / demo_observations",
                       _leasing_comps_impl, deal)
        observed_rent_psf        = leasing.get("observed_rent_psf")
        observed_rent_psf_source = leasing.get("source")

        underwriting = tool(
            "Underwriting Delta", "Deterministic NOI/IRR math",
            _underwriting_impl, deal,
            observed_rent_psf=observed_rent_psf,
            observed_cap=None,
            observed_rate_bps=observed_rate_bps,
            observed_rent_psf_source=observed_rent_psf_source,
            observed_rate_source=observed_rate_source,
        )

        dispatch = tool("POST_MATH_DISPATCH", "IRR band classification",
                        _dispatch_impl, underwriting)

        state.emit("band", {
            "band":       dispatch["band"],
            "edge_label": dispatch.get("edge_label"),
            "reason":     dispatch.get("dispatch_reason"),
            "irr":        dispatch.get("irr_observed_pct"),
        })

        # Snapshot
        state.emit("tool_start", {"tool": "Snapshot Write", "detail": "Persisting tool outputs"})
        snap_path = _write_snapshot(deal["deal_id"], {
            "violations":    hpd,
            "market_signals": acris,
            "macro_signals": fred,
            "leasing_comps": leasing,
            "underwriting":  underwriting,
            "dispatch":      dispatch,
        })
        state.emit("tool_done", {"tool": "Snapshot Write", "result": {"path": str(snap_path)}})

        state.emit("tool_start", {"tool": "Snapshot Diff", "detail": "Comparing vs. yesterday"})
        snapshot_diff = json.loads(_snapshot_diff_impl(deal["deal_id"], snapshots_dir=SNAPSHOTS_DIR))
        state.emit("tool_done", {"tool": "Snapshot Diff", "result": snapshot_diff})

        signals = {
            "hpd": hpd, "acris": acris, "fred": fred,
            "leasing": leasing, "snapshot_diff": snapshot_diff,
            "underwriting": underwriting, "dispatch": dispatch,
            "observed_inputs_used": {
                "observed_rent_psf":        observed_rent_psf,
                "observed_rent_psf_source": observed_rent_psf_source,
                "observed_rate_bps":        observed_rate_bps,
                "observed_rate_source":     observed_rate_source,
            },
        }

        # Checkpoint
        band = dispatch.get("band", "red")
        checkpoint_outcome = "no checkpoint required (band not red)"

        if band == "red":
            state.emit("checkpoint", {
                "reason":    dispatch.get("dispatch_reason"),
                "irr_observed": underwriting.get("irr_observed_pct"),
                "irr_assumed":  underwriting.get("irr_assumed_pct"),
                "irr_delta":    underwriting.get("irr_delta_pct"),
                "noi_delta":    underwriting.get("noi_delta_dollars"),
                "drivers":      underwriting.get("drivers", []),
            })
            try:
                decision = state.checkpoint_q.get(timeout=300)
            except queue.Empty:
                decision = "abort"

            if decision == "abort":
                state.emit("abort", {"message": "Analyst aborted. No briefing produced."})
                state.close()
                return

            if decision == "downgrade":
                d = dict(dispatch)
                d["band"]             = "yellow"
                d["edge_label"]       = "advisory"
                d["dispatch_reason"]  = f"analyst-downgraded from red. original: {dispatch.get('dispatch_reason', '')}"
                d["analyst_downgrade"] = True
                signals["dispatch"]   = d
                checkpoint_outcome    = "analyst downgraded red -> yellow"
            else:
                checkpoint_outcome = "auto-confirmed red (bypassed)"

            state.emit("checkpoint_resolved", {"decision": decision, "outcome": checkpoint_outcome})

        # LLM — Synthesis
        state.emit("llm_start", {"phase": "synthesis", "message": "Claude drafting analyst briefing..."})

        model_id = os.getenv("MODEL_ID", "anthropic/claude-sonnet-4-6")
        from strands.models.litellm import LiteLLMModel
        from strands import Agent

        params: dict = {"max_tokens": 8192, "temperature": 0.2}
        model = LiteLLMModel(model_id=model_id, params=params)
        agent = Agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT, callback_handler=None)

        raw_synth = str(agent(_synthesis_prompt(deal, signals, checkpoint_outcome)))

        # Token tracking (best-effort via strands accumulated_usage)
        try:
            usage = agent.state.accumulated_usage
            state.token_usage["input"]  = usage.get("inputTokens", 0)
            state.token_usage["output"] = usage.get("outputTokens", 0)
        except Exception:
            pass

        state.emit("synthesis", {"content": raw_synth, "tokens": dict(state.token_usage)})

        # LLM — Compound findings
        state.emit("llm_start", {"phase": "compound", "message": "Claude drafting compound findings..."})
        raw_compound = str(agent(_compound_prompt(deal, signals)))

        try:
            usage = agent.state.accumulated_usage
            state.token_usage["input"]  = usage.get("inputTokens", 0)
            state.token_usage["output"] = usage.get("outputTokens", 0)
        except Exception:
            pass

        state.emit("compound", {"content": raw_compound, "tokens": dict(state.token_usage)})
        state.emit("complete", {"tokens": dict(state.token_usage)})

    except Exception as exc:
        state.emit("error", {"message": str(exc), "detail": traceback.format_exc()})
    finally:
        state.close()


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

WEB_DIR = str(Path(__file__).resolve().parent)

@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/slides")
def slides():
    return send_from_directory(WEB_DIR, "slides.html")


@app.route("/deals")
def list_deals():
    deals = []
    for p in sorted(DEALS_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            deals.append({
                "deal_id":    d["deal_id"],
                "address":    d["property"]["address"],
                "submarket":  d["property"].get("submarket", ""),
                "asset_class": d["property"].get("asset_class", ""),
                "deal_stage": d.get("deal_stage", ""),
                "irr_assumed": d["underwriting"]["irr"],
            })
        except Exception:
            pass
    return jsonify(deals)


@app.route("/run", methods=["POST"])
def start_run():
    data    = request.get_json(force=True)
    deal_id = data.get("deal_id", "")
    deal_path = DEALS_DIR / f"{deal_id}.json"
    if not deal_path.exists():
        return jsonify({"error": f"Deal not found: {deal_id}"}), 404

    deal   = json.loads(deal_path.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:8]
    state  = RunState(run_id, deal_id)
    _runs[run_id] = state

    threading.Thread(target=_run_agent, args=(state, deal), daemon=True).start()
    return jsonify({"run_id": run_id})


@app.route("/checkpoint/<run_id>", methods=["POST"])
def send_checkpoint(run_id: str):
    state = _runs.get(run_id)
    if not state:
        return jsonify({"error": "run not found"}), 404
    decision = request.get_json(force=True).get("decision", "abort")
    state.checkpoint_q.put(decision)
    return jsonify({"ok": True})


@app.route("/stream/<run_id>")
def stream(run_id: str):
    state = _runs.get(run_id)
    if not state:
        return jsonify({"error": "run not found"}), 404

    def generate():
        while True:
            try:
                item = state.q.get(timeout=120)
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"
                continue
            if item is None:
                yield "data: {\"type\":\"done\"}\n\n"
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print("CRE Deal Pulse v2 — Web UI")
    print("Open: http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
