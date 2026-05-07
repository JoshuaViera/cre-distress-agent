# CRE Deal Pulse

An AI agent that monitors live market, property, and macro signals against a CRE analyst's deal underwriting and tells them — in plain English — when reality is breaking their assumptions and what to do about it.

> _"Market rent in Midtown South dropped to $68/SF this week. Your deal assumes $74. At $68, your NOI falls by $180K and your IRR drops from 14% to 11%. Recommend re-running underwriting before proceeding to LOI."_

That's the product. The analyst didn't have to check CoStar, read three articles, or update their model. The agent did it and told them what it means.

---

## Why this exists

A junior CRE acquisitions analyst spends hours every day refreshing the same sources — CoStar, FRED, news feeds, NYC city portals — to check whether the deals already in their pipeline still pencil. Rates moved, a comp traded, a violation hit the target property: any one signal can change the underwriting. The integration happens in the analyst's head, across five tabs, and material changes get caught late.

Deal Pulse replaces "five tabs and a spreadsheet" with a one-page briefing. It loads the analyst's deal assumptions, scans three buckets of public data, computes the dollar and IRR impact deterministically in Python, and produces a markdown briefing — routed through a deterministic dispatch node that decides whether the deal needs human review or can auto-render.

Built for the Pursuit AI-Native Fellowship Cycle 3.

---

## How it works (v2 architecture)

**Input:** a deal profile JSON (address, BBL, asset class, underwriting assumptions).

**Five signal tools (Python orchestrates, model never calls):**

| Bucket           | Source                  | What it watches                                              |
| ---------------- | ----------------------- | ------------------------------------------------------------ |
| Property         | NYC HPD violations API  | Open code violations, severity breakdown on the target asset |
| Market — sales   | NYC ACRIS               | Recent comparable sales by borough and submarket             |
| Market — leasing | `tools/leasing_comps.py`| SF-weighted observed rent_psf from CSV / demo_observations   |
| Macro            | FRED                    | 10-year Treasury, SOFR                                       |
| Underwriting     | Pure Python DCF         | Deterministic NOI / IRR delta + 5-year levered Newton's IRR  |

**The v2 control flow:**

```
gather_signals
   -> POST_MATH_DISPATCH (absolute observed IRR -> green / yellow / red)
       |
       +-- green  (IRR > 10%)        -> auto_render          -> synthesis
       +-- yellow (7.5%-10%)         -> advisory             -> synthesis
       +-- red    (IRR < 7.5%)       -> requires_human_review
                                          -> human checkpoint
                                              +-- y (confirmed) -> synthesis as red
                                              +-- d (downgrade) -> band -> yellow -> synthesis
                                              +-- q (abort)     -> exit, no briefing
   -> synthesis turn         (Claude, single LLM call, branches tone on edge_label)
   -> compound_finding turn  (Claude, second LLM call, cross-signal reasoning)
   -> final markdown briefing
```

**Stack:** Strands (agent framework) · LiteLLM (provider adapter) · OpenRouter or Anthropic gateway · Claude (v2 brain — Hy3 free tier still works for v1-equivalent runs) · Python 3.10+.

---

## Quick start

**Prereqs:** Python 3.10+ (3.13 recommended), an OpenRouter or Anthropic API key, a free FRED API key.

```bash
git clone https://github.com/JoshuaViera/cre-distress-agent.git
cd cre-distress-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

```
OPENROUTER_API_KEY=your_key_here
FRED_API_KEY=your_key_here
# Optional: swap the model. Defaults to Hy3 free tier when unset.
MODEL_ID=openrouter/anthropic/claude-sonnet-4-5
```

Run the agent on the staged demo deal:

```bash
python agent.py
```

You should see the agent call HPD, ACRIS, FRED, leasing_comps, and the deterministic underwriting math in sequence, then POST_MATH_DISPATCH classifies the band, the agent pauses at any RED-band checkpoint (stdin `y`/`d`/`q`), then prints the markdown briefing followed by the compound findings section.

To point at a different deal profile:

```bash
python agent.py --deal deals/midtown-south-office-001.json
```

To override observed market rent (otherwise pulled from `leasing_comps`):

```bash
python agent.py --observed-rent 68
```

To auto-confirm any RED-band checkpoint (for unattended runs):

```bash
python agent.py --yes
```

Verify each tool independently:

```bash
python tools/violations.py
python tools/market_signals.py
python tools/macro_signals.py     # needs FRED_API_KEY
python tools/underwriting.py
python tools/dispatch.py
python tools/leasing_comps.py
python tools/snapshot_diff.py
```

Run the full test suite (no API keys required — strands, requests, and stdin are all stubbed):

```bash
pytest tests/ -v
```

---

## Deal profile

The deal profile is the single shared input. Every tool and the math function reads from this schema:

```json
{
  "deal_id": "midtown-south-office-001",
  "property": {
    "address": "150 W 30th St, New York, NY 10001",
    "bbl": "1008060001",
    "borough": "1",
    "submarket": "Midtown South",
    "asset_class": "office",
    "square_footage": 85000,
    "top_tenants": ["Tenant A", "Tenant B", "Tenant C"]
  },
  "deal_stage": "LOI",
  "underwriting": {
    "market_rent_psf": 74.0,
    "in_place_rent_psf": 68.0,
    "vacancy_rate": 0.08,
    "going_in_cap": 0.055,
    "exit_cap": 0.06,
    "rent_growth": 0.03,
    "hold_period_years": 5,
    "noi": 5200000,
    "irr": 0.14
  },
  "assumptions_locked_at": "2026-04-25"
}
```

**Field contract:** rates and percentages are decimals (0.14, not "14%"). BBL is a 10-digit string (1 borough + 5 block + 4 lot). Borough is a single character "1"–"5".

**Optional leasing comps source (v2):** drop a CSV at `deals/<deal_id>.comps.csv` with columns `lease_date,address,rent_psf,sf,tenant`. The leasing_comps tool will SF-weight it and prefer it over `demo_observations`.

---

## Repo layout

```
cre-distress-agent/
├── agent.py                       # Main loop — gather, dispatch, checkpoint, synthesis, compound
├── tools/
│   ├── violations.py              # NYC HPD violations (Property)
│   ├── market_signals.py          # NYC ACRIS sales comps (Market — sales)
│   ├── leasing_comps.py           # Leasing comps resolver (Market — leasing) [v2]
│   ├── macro_signals.py           # FRED 10Y Treasury + SOFR (Macro)
│   ├── underwriting.py            # Deterministic NOI/IRR delta math
│   ├── dispatch.py                # POST_MATH_DISPATCH band classifier [v2]
│   └── snapshot_diff.py           # Day-over-day diff vs prior snapshot
├── prompts/
│   ├── system_prompt_v2.txt       # Synthesis-only system prompt (no scoring)
│   └── compound_finding.txt       # Cross-signal compound finding prompt [v2]
├── deals/
│   └── midtown-south-office-001.json
├── tests/
│   ├── conftest.py                # Strands stub, fake_requests, tmp dirs, demo_deal
│   ├── test_dispatch.py           # 18 cases: band boundaries, fail-safes, IRR echo
│   ├── test_leasing_comps.py      # 8 cases: override / CSV / demo / unavailable
│   ├── test_checkpoint.py         # 25 cases: state machine, audit log, immutability
│   └── test_run_loop.py           # 6 integration: green / yellow / red / confirm / downgrade / abort
├── scripts/
│   └── record_demo.sh             # Wraps agent.py in script(1) for transcript
├── runs/                          # Auto-created; checkpoint decision logs (gitignored)
├── snapshots/                     # Auto-created; daily ground-truth snapshots (gitignored)
├── test_model.py                  # LLM round-trip smoke test (honors MODEL_ID)
├── requirements.txt
├── .env.example
├── README.md
├── CODEBASE_AUDIT.md              # Full-repo audit
└── CURRENT_STATE.md               # Internal architecture/state snapshot
```

---

## v1 scope (shipped)

- Three signal tools: HPD, ACRIS, FRED
- Deterministic underwriting delta math
- Materiality scoring (1–5) by the LLM
- Human checkpoint on severity-5 alerts
- Markdown briefing to stdout
- Single staged demo deal

## v2 scope (this branch)

Shipped:

- [x] **POST_MATH_DISPATCH band classifier** — pure Python, classifies on absolute observed IRR (green > 10% / yellow 7.5%–10% / red < 7.5%). The model no longer scores materiality; math does.
- [x] **Three-outcome human checkpoint** — `y` confirmed, `d` downgrade (red → yellow), `q` abort. Audit log captures dispatch reason, IRR, NOI delta, drivers, decision.
- [x] **Synthesis turn (Claude)** — single LLM call, branches tone on edge_label (`auto_render` / `advisory` / `requires_human_review`). Replaces v1's two-phase scoring + briefing.
- [x] **Compound_finding turn (Claude)** — third LLM call, cross-signal reasoning over today's signals + snapshot_diff temporal vectors. Hard constraint: must cite ≥2 named signals + the linking diff field, otherwise emits "No compound findings this run."
- [x] **Leasing comps as named tool** — `tools/leasing_comps.py`. Resolution order: CLI override → `deals/<id>.comps.csv` (SF-weighted) → `demo_observations` → unavailable.
- [x] **Snapshot diff temporal frame** — captures `dispatch` + `leasing_comps` alongside the four signal tools so band transitions are first-class change vectors.
- [x] **Model swap via env** — `MODEL_ID` env var; one-line .env change to flip Hy3 → Claude.
- [x] **Hermetic test suite** — 58 tests, no API keys required (strands stubbed, requests faked, stdin/dirs redirected to tmp).

Not yet:

- Override-log writer/loader for analyst memory across runs (Tier 3).
- Real planner: agent decides which sources to check for this deal, not all of them every run.
- Slack/email delivery instead of stdout.
- Tenant credit watch (SEC EDGAR).

---

## Design decisions worth flagging

**Math is the scorer.** The dispatch tool classifies the deal into green / yellow / red based on the absolute observed IRR. The model does not output severity scores. This is the load-bearing change between v1 and v2 — the model's job is synthesis and cross-signal reasoning, not classification.

**Dispatch is fail-safe red.** If underwriting returns an error envelope, or `irr_observed_pct` is missing, dispatch defaults to red so the analyst always sees the issue at the checkpoint rather than silently auto-rendering a broken brief.

**Bands route on level, not delta.** A deal that moves 3pt and stays above 10% IRR is green (still a good deal). A deal that moves 1pt and crosses 7.5% is red (the deal is now bad). v1's `_severity_from_irr_delta` (delta magnitude + deal stage) is preserved in `tools/underwriting.py` for back-compat but is no longer load-bearing.

**Checkpoint between dispatch and synthesis.** Stages 4–5 (synthesis + compound_finding) are LLM-only. The human checkpoint sits between stage 3 (dispatch) and stage 4 (synthesis), which resolves the v2 diagram's "no human in the loop at stage 5" requirement without contradiction.

**Leasing comps is a named tool.** Not a hidden lookup inside the agent loop. v1 plumbed `demo_observations` directly into underwriting; v2 routes it through `tools/leasing_comps.py` so the architecture diagram has a real node and a CSV upgrade path is one file away.

**Compound findings have hard guardrails.** Without 1–5 scoring the model could speculate. The compound_finding prompt requires ≥2 named signals and a snapshot_diff anchor per finding, otherwise it emits the empty-state line. No speculative cross-signal narratives.

**No paid data.** NYC has the strongest free public real estate data in the US (HPD, ACRIS, DOF on Socrata). FRED is free. This is defensible without a Bloomberg or CoStar subscription.

---

## Team

| Person            | Role                                                  |
| ----------------- | ----------------------------------------------------- |
| Joshua Viera      | Engineering lead, integration owner, final audit      |
| Pedro Martins     | Tool 2 — Market signals (ACRIS)                       |
| Elliot Chen       | Tool 3 — Macro signals (FRED), demo slide advancement |
| Kevin Natera      | Slides, integration QA                                |
| Gamaliel Leguista | Merges, integration QA, presenting                    |

---

## Known model behaviors

- **Hy3 (v1 baseline) leaks reasoning text** unless `extra_body.reasoning.exclude=True`. `max_tokens` must be ≥ 8192 (currently 16384) because reasoning tokens count against the budget. LiteLLM's `reasoningContent is not supported in multi-turn` warning is harmless and ignored.
- **Claude (v2)** is a clean fit for synthesis + compound_finding. The `extra_body.reasoning` block is harmless when sent to Claude (LiteLLM ignores it); leaving it in lets you flip back to Hy3 by changing only `MODEL_ID`.
- **NYC Open Data has no auth** but rate-limits exist. Caching is a v3 concern.
- **Snapshot dates are UTC.** `snapshots/<deal_id>/{YYYY-MM-DD}.json` uses UTC date for the filename, so analysts in non-UTC timezones may see "today" roll over at a different local time.

---

## License

MIT — fellowship project, repo is public for demo purposes.
