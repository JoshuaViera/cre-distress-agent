# CRE Deal Pulse — Current State Snapshot

> This document is a self-contained briefing for a Claude assistant that does not have access to the repository or GitHub. It describes the project as it exists on the `feat-jv-v1-final` branch as of 2026-05-06 (refreshed at end of v2 implementation — POST_MATH_DISPATCH, three-outcome checkpoint, synthesis + compound_finding LLM turns, leasing_comps named tool, hermetic test suite). After reading this, you should know what's built, what's not, and have enough context to plan next steps.

> **v2 architecture summary (load-bearing):**
> 1. The model **does not score severity**. The `tools/dispatch.py` POST_MATH_DISPATCH classifier maps **absolute observed IRR** to a band (green > 10% / yellow 7.5%–10% / red < 7.5%), and that band is the only materiality signal.
> 2. **Three checkpoint outcomes**: `y` (confirmed → synthesis as red), `d` (downgrade → band rewritten to yellow → synthesis), `q` (abort → exit, no briefing). Audit log captures dispatch reason, IRR, NOI delta, drivers, decision.
> 3. **Two LLM turns**: synthesis (single call, branches tone on edge_label) and compound_finding (cross-signal reasoning over today's signals + snapshot_diff). Replaces v1's two-phase scoring + briefing.
> 4. **Diagram contradiction resolved**: human checkpoint sits between stage 3 (dispatch) and stage 4 (synthesis). Stages 4–5 are LLM-only.

---

## 1. What this project is

**CRE Deal Pulse** is an AI agent for a NYC commercial real estate (CRE) acquisitions analyst. Given a deal profile with locked underwriting assumptions, it scans live public data sources, computes the dollar/IRR impact of any divergence between observed reality and the analyst's assumptions, and produces a markdown briefing scored by materiality.

**Killer-quote scenario** the demo is tuned to hit:

> "Market rent in Midtown South dropped to $68/SF this week. Your deal assumes $74. At $68, your NOI falls by ~$180K and your IRR drops from 14% to 11%. Recommend re-running underwriting before proceeding to LOI."

**Origin:** built for the Pursuit AI-Native Fellowship Cycle 3.

**GitHub:** https://github.com/JoshuaViera/cre-distress-agent (public). Default branch `main`.

---

## 2. Tech stack

- **Python 3.10+** (3.13 recommended, in use)
- **[Strands Agents](https://github.com/strands-agents/sdk-python)** — agent framework (`strands-agents[litellm]>=1.37.0,<2.0.0`)
- **LiteLLM** — model provider adapter
- **OpenRouter** — gateway
- **Tencent Hunyuan-3 Preview (`openrouter/tencent/hy3-preview:free`)** — current LLM (free reasoning model)
- **`requests`** + **`python-dotenv`** — only other prod deps

No database. No web framework. Pure CLI app. Output is markdown printed to stdout.

---

## 3. Repo layout (current)

```
cre-distress-agent/
├── agent.py                                    # Main loop. Gather → dispatch → checkpoint → synthesis → compound.
├── tools/
│   ├── violations.py                           # Tool 1: NYC HPD violations API (Property signals)
│   ├── market_signals.py                       # Tool 2: NYC ACRIS DEED/DEEDO comps (Market signals — sales)
│   ├── leasing_comps.py                        # Tool 7: leasing comps resolver (Market — leasing) [v2]
│   ├── macro_signals.py                        # Tool 3: FRED 10Y Treasury + SOFR (Macro signals)
│   ├── underwriting.py                         # Tool 4: deterministic NOI/IRR delta math
│   ├── dispatch.py                             # Tool 6: POST_MATH_DISPATCH band classifier [v2]
│   └── snapshot_diff.py                        # Tool 5: day-over-day diff vs prior snapshot
├── prompts/
│   ├── system_prompt_v2.txt                    # Synthesis-only system prompt (no scoring)
│   └── compound_finding.txt                    # Cross-signal compound finding prompt [v2]
├── deals/
│   └── midtown-south-office-001.json           # Staged demo deal (the only deal currently in the repo)
├── tests/                                      # Hermetic pytest suite (58 tests, no API keys required) [v2]
│   ├── conftest.py                             # Strands stub, fake_requests fixture, tmp dirs, demo_deal
│   ├── test_dispatch.py                        # 18 cases: band boundaries, fail-safes, IRR echo
│   ├── test_leasing_comps.py                   # 8 cases: override / CSV / demo / unavailable / malformed
│   ├── test_checkpoint.py                      # 25 cases: state machine, audit log, immutability
│   └── test_run_loop.py                        # 6 integration: green / yellow / red / confirm / downgrade / abort
├── scripts/
│   └── record_demo.sh                          # Wraps `agent.py` in `script(1)` for a recorded transcript
├── runs/                                       # Auto-created at runtime; checkpoint decision logs (gitignored)
├── snapshots/                                  # Auto-created at runtime; per-deal daily ground-truth snapshots (gitignored)
├── test_model.py                               # LLM round-trip smoke test (honors MODEL_ID — Hy3 or Claude)
├── requirements.txt                            # Includes pytest as dev/test dep
├── .env.example                                # OPENROUTER_API_KEY + FRED_API_KEY + MODEL_ID (placeholders + Claude swap notes)
├── .gitignore                                  # .env, .venv/, __pycache__/, runs/, snapshots/, .DS_Store
├── CODEBASE_AUDIT.md                           # Full-repo audit
├── CRE Deal Pulse — PRD.pdf                    # Product requirements doc (binary, not parsed here)
└── README.md
```

The `tests/` directory is hermetic: `strands` is stubbed, `requests` is mocked via the `fake_requests` fixture, and `RUNS_DIR` / `SNAPSHOTS_DIR` are redirected to `tmp_path`. The full suite runs in &lt;1 second with no API keys. Per-tool `__main__` smoke tests still work for ad-hoc runs and live integration checks.

---

## 4. The agent flow (how it actually runs — v2)

The control flow is **Python-driven**. The model never calls tools — it only reads structured tool outputs that Python collected. v2 introduces a deterministic dispatch node that routes the agent past or through the human checkpoint based on the absolute observed IRR.

```
run(deal_path, observed_rent_override, auto_confirm)
  ├── _load_deal()                  → parses deal JSON
  ├── _gather_signals()             → Python orchestrates everything:
  │     ├── violations._impl(bbl)
  │     ├── market_signals._impl(borough_name)
  │     ├── macro_signals._impl(days_back=30)         # observed_rate_bps from treasury_10y.bps_change
  │     ├── leasing_comps._impl(deal, override)       # v2: CSV → demo_observations → unavailable
  │     ├── underwriting._impl(deal, observed_rent_psf, observed_cap, observed_rate_bps, ...)
  │     ├── dispatch._impl(underwriting)              # v2: POST_MATH_DISPATCH band classifier
  │     ├── _write_snapshot(deal_id, tool_outputs)    # snapshot now includes leasing_comps + dispatch
  │     └── snapshot_diff._impl(deal_id, snapshots_dir=SNAPSHOTS_DIR)
  ├── if dispatch.band == "red":
  │     └── _human_checkpoint(deal, dispatch, underwriting, auto_confirm)
  │           → returns {"decision": "confirmed" | "downgrade" | "abort", ...}
  │           → logs to runs/<UTC-timestamp>.log with full forensic context
  │
  │           if "abort":     return (no briefing)
  │           if "downgrade": dispatch = _downgrade_dispatch(dispatch)  # band → yellow
  │           if "confirmed": continue as red
  ├── SYNTHESIS  — agent(_synthesis_query)            → markdown briefing, branches tone on edge_label
  └── COMPOUND   — agent(_compound_finding_query)     → cross-signal section appended to briefing
```

Key design decisions baked into this (v2):

- **Math is the materiality signal.** The dispatch tool classifies on `irr_observed_pct` (absolute level, not delta magnitude). The LLM does not output severity scores. The system prompt (`prompts/system_prompt_v2.txt`) explicitly forbids it.
- **Dispatch is fail-safe red.** If underwriting returns an error envelope, or `irr_observed_pct` is missing, dispatch defaults to red. The analyst always sees the issue at the checkpoint instead of silently auto-rendering a broken brief.
- **Bands route on level, not delta.** Green > 10% IRR. Yellow 7.5%–10% (boundaries belong to yellow). Red < 7.5%. The v1 `_severity_from_irr_delta` (delta + deal stage) is preserved in `tools/underwriting.py` for back-compat but is no longer load-bearing.
- **Three checkpoint outcomes.** `y` confirmed (proceed as red), `d` downgrade (band → yellow → proceed as advisory), `q` abort (exit, no briefing). Legacy `n` is accepted as an alias for downgrade per the v2 spec wording. Two re-prompts on bad input, then default to abort.
- **`_downgrade_dispatch` is immutable.** Returns a copy; the original snapshot already on disk stays as-captured for forensic replay. The downgraded copy carries `analyst_downgrade: true` and the original reason inside `dispatch_reason`.
- **Two LLM turns, both Claude-friendly.** Synthesis is a single call that branches tone via `_EDGE_TONE[edge_label]` (`auto_render` / `advisory` / `requires_human_review`). Compound_finding is a separate call with hard guardrails: must cite ≥2 named signals + the snapshot_diff anchor, otherwise emits "No compound findings this run."
- **Diagram contradiction resolved.** Stages 4–5 (synthesis + compound_finding) are LLM-only. The human checkpoint sits between stage 3 (dispatch) and stage 4 (synthesis), satisfying both "no human in the loop at stage 5" and "red path requires human review."
- **Snapshots contain ground-truth observations only.** v2 snapshot adds `leasing_comps` and `dispatch` alongside the four signal tools, so band transitions (e.g., yellow → red) appear as first-class change vectors in the day-over-day diff.
- **Snapshot diff feeds the signals block.** Three render modes minimize first-run drift (first-run note, unchanged note, populated JSON). Same as v1 — kept stable.
- **`callback_handler=None` + LiteLLM logging suppressed.** Demo terminal shows only explicit status lines + final briefing. The `extra_body.reasoning.exclude` block is harmless when sent to Claude; Hy3 needs it.
- **`MODEL_ID` env var.** One-line `.env` swap between Hy3 and Claude. `test_model.py` honors the same var so the smoke test stays in parity.

---

## 5. The deal profile schema

Single shared input, lives at `deals/midtown-south-office-001.json`:

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
    "exit_cap": 0.060,
    "rent_growth": 0.03,
    "hold_period_years": 5,
    "noi": 5200000,
    "irr": 0.14
  },
  "financing": { "ltv": 0.75, "debt_rate": 0.045 },
  "market_dynamics": { "annual_rent_rollover_pct": 0.385 },
  "demo_observations": {
    "observed_rent_psf": 68.0,
    "_simulated_signal": "Three Midtown South office leases week of 2026-04-26 signed at $66-69/SF, weighted midpoint $68."
  },
  "assumptions_locked_at": "2026-04-25"
}
```

**Field contract:**
- All percentages are decimals (`0.14` not `"14%"`)
- `bbl` is a 10-digit string: 1 borough + 5 block + 4 lot
- `property.borough` is a single character `"1"`–`"5"` (1=Manhattan, 2=Bronx, 3=Brooklyn, 4=Queens, 5=Staten Island)
- `demo_observations.observed_rent_psf` is the staged signal that drives the killer-quote scenario. In production this would be replaced by a live leasing-comps feed (CompStak, etc.).

There is **only one deal profile** in the repo. Multi-deal portfolio handling does not exist yet.

---

## 6. The seven tools (current capabilities, v2)

All seven return **JSON strings** with a consistent error envelope pattern. Errors include `{"error": "<code>", "message": "..."}` so the agent can narrate failure rather than crash. Tools 6 and 7 are v2 additions.

### Tool 1 — `tools/violations.py` :: `get_property_distress_signals(bbl: str)`
- **Source:** NYC HPD Housing Maintenance Code Violations API (Socrata `wvxf-dwi5`)
- **Input:** 10-digit BBL string
- **Output keys:** `bbl`, `open_violations_count`, `severity_breakdown {A,B,C,I}`, `most_recent_violation`, `distress_score` (`none|low|medium|high`), `sample_violations[]`, `source_url`
- **Heuristic:** `class_c >= 3 OR total >= 20` → `high`; `class_c >= 1 OR total >= 10` → `medium`; `total > 0` → `low`; else `none`
- **No auth required.** 10s timeout. Validates BBL format before hitting API.

### Tool 2 — `tools/market_signals.py` :: `get_market_signals(borough, days_back=90, min_sale_price=1_000_000)`
- **Source:** NYC ACRIS Master (`bnx9-e6tj`) + ACRIS Legals (`8h5j-fqxa`) — two-trip "JOIN" because Socrata can't cross-dataset join
- **Input:** borough name (case-insensitive: Manhattan/Bronx/Brooklyn/Queens/Staten Island), lookback days, minimum sale price
- **Filters:** `doc_type IN ('DEED','DEEDO')`, `recorded_borough` enforced on **both** the Master query and the Legals enrichment query (this was a real bug — fixed in commit `50b8fb9`)
- **Output keys:** `sale_count`, `median_price`, `sample_sales[]` (top 5, enriched with address + ground-truth borough_code from Legals), `market_signal` (`active|slow|no_data`), `source_url`
- **Heuristic:** `sale_count >= 10` → `active`; `>= 1` → `slow`; `0` → `no_data`
- **No auth required.** 15s timeout. ACRIS can be slow.

### Tool 3 — `tools/macro_signals.py` :: `get_macro_signals(days_back=30)`
- **Source:** FRED API — series `DGS10` (10Y Treasury) and `SOFR`
- **Input:** lookback days (default 30)
- **Output keys:** `macro_signal` (`rates_moved|stable|no_data|error`), `treasury_10y {current_value, prior_value, bps_change, ...}`, `sofr {...}`, `narrative`, `source_url_treasury_10y`, `source_url_sofr`
- **Sign convention:** `bps_change = current - prior`. Positive = rates rose (bad for deal). Negative = rates fell (good for deal).
- **Threshold:** `|bps_change| >= 25` → `rates_moved`
- **Auth:** requires `FRED_API_KEY` (free). Missing key returns clean error envelope.
- **Date-window logic:** pads the request window by 21 days because FRED skips weekends/holidays — without padding, `lookback_days` alone often doesn't contain enough trading days to find a clean prior observation.

### Tool 4 — `tools/underwriting.py` :: `compute_underwriting_delta(deal_profile, observed_rent_psf, observed_cap, observed_rate_bps, observed_rent_psf_source=None, observed_cap_source=None, observed_rate_source=None)`
- **Pure Python.** No network. The LLM does no arithmetic — it quotes these numbers.
- **NOI delta formula:**
  ```
  noi_delta = SF * (observed_rent - assumed_rent) * (1 - vacancy) * annual_rollover_pct
  ```
  Only the rolling portion (default `_DEFAULT_ROLLOVER_PCT = 0.385`) re-prices this year. This is what makes the $74→$68 scenario land at ~$180K instead of the full ~$470K gross.
- **IRR via Newton's method.** 5-year levered DCF. Builds two cash-flow vectors (assumed and observed), solves IRR for each, returns delta. `purchase_price = noi_assumed / going_in_cap` is **locked at acquisition** — the observed scenario keeps the same purchase price (and equity stack) and only re-marks NOI / exit cap / debt rate. Recomputing purchase price from observed NOI would silently zero out the IRR delta.
- **Severity floor (`_severity_from_irr_delta`):**
  - `|irr_delta| >= 2.5pp` AND deal_stage in {LOI, UNDER_CONTRACT} → **5**
  - `|irr_delta| >= 2.5pp` (other stages) → 4
  - `>= 1.5pp` → 4
  - `>= 0.5pp` → 3
  - `>= 0.1pp` → 2
  - else → 1
- **Default financing fallback:** if `deal_profile.financing` is missing — `LTV=0.72`, `debt_rate=0.040`, `rollover_pct=0.385` (tuned so the staged deal hits ~14% baseline IRR and ~3pt drop on the killer quote).
- **Output keys:** `deal_id`, `assumptions_locked_at` (Tier 1c — echoed for audit), `drivers[]` (Tier 1c — per-observed-value entries with `name`, `observed`, `assumed`, `unit`, `source_tool`), `noi_delta_dollars`, `noi_assumed_dollars`, `noi_observed_dollars`, `irr_assumed_pct`, `irr_observed_pct`, `irr_delta_pct`, `irr_stated_in_deal_pct`, `exit_value_delta_dollars`, `severity_hint`, `narrative_inputs {...}`
- **Source kwargs (Tier 1c):** Optional provenance labels per observed value. `agent.py` passes `observed_rent_psf_source` as `"cli_override"` or `"deal.demo_observations"` based on where the value came from, and `observed_rate_source` as `"macro_signals"` when FRED produced the rate change.

### Tool 5 — `tools/snapshot_diff.py` :: `compare_to_yesterday(deal_id, snapshots_dir=None)`
- **Pure Python**, reads from disk. No network.
- **What it does:** Lists snapshot files under `snapshots/{deal_id}/`, sorts descending, skips today's date, takes the most recent prior. Loads both today's and prior snapshots, walks the `tool_outputs` trees, emits field-level diffs.
- **Diffable fields:** numeric leaves and three signal-state categoricals (`market_signal`, `macro_signal`, `distress_score`).
- **Skipped fields:** source URLs, free-text narratives, sample lists (`sample_violations`, `sample_sales`, `drivers`, `narrative_inputs`), per-run timestamps (`most_recent_violation`, `captured_at`), snapshot metadata (`snapshot_version`, `deal_id`, `assumptions_locked_at`), deal-constant identifiers (`bbl`, `borough`, `borough_code`).
- **Generic walker:** Adding a new signal tool later does not require touching this code; new top-level keys appear automatically as `new_signals` on the first day they show up. v2's `dispatch` and `leasing_comps` were absorbed by this property without code changes.
- **Output keys:** `deal_id`, `diff_summary[]` (each entry: `field`, `yesterday`, `today`, `delta`), `new_signals[]`, `dropped_signals[]`, `no_change`, `compared_against` (ISO date or null), optional `note` for empty-state explanations.
- **Empty cases:** No prior snapshot for the deal → `no_change=true`, `compared_against=null`, `note="no prior snapshot — first run for this deal_id"`. Prior exists but identical → `no_change=true`, `compared_against=<date>`, no note.

### Tool 6 — `tools/dispatch.py` :: `post_math_dispatch(underwriting_result)` [v2]
- **Pure Python**, no network. Reads `irr_observed_pct` from a parsed underwriting envelope.
- **Bands (absolute observed IRR, locked contract):**
  - `irr > 0.10` → **green** (`auto_render`)
  - `0.075 <= irr <= 0.10` → **yellow** (`advisory`) — both boundaries belong to yellow
  - `irr < 0.075` → **red** (`requires_human_review`)
- **Fail-safe red:** missing IRR, error envelope upstream, or non-dict input → defaults to red so the analyst always sees the issue at the checkpoint instead of a silent auto-render.
- **Output keys:** `band`, `edge_label`, `irr_observed_pct`, `irr_assumed_pct`, `irr_delta_pct`, `thresholds {green_above, red_below}`, `dispatch_reason` (one-line human-readable), optional `error` / `message`.
- **Constants exported:** `GREEN_THRESHOLD=0.10`, `RED_THRESHOLD=0.075`, `EDGE_AUTO_RENDER`, `EDGE_ADVISORY`, `EDGE_REQUIRES_HUMAN_REVIEW`. Tests import these directly.

### Tool 7 — `tools/leasing_comps.py` :: `get_leasing_comps(deal, override_rent_psf=None, deals_dir=None)` [v2]
- **Pure Python**, no network. Promotes the v1 `deal["demo_observations"]["observed_rent_psf"]` lookup into a named tool with a real CSV upgrade path.
- **Resolution order** (highest priority wins):
  1. `override_rent_psf` (caller-passed; `agent.py` passes it from `--observed-rent` CLI flag)
  2. `deals/{deal_id}.comps.csv` if present, with columns `lease_date,address,rent_psf,sf,tenant`. Returns the SF-weighted average of all rows with positive `rent_psf`. If no SF column or all SFs missing, falls back to a simple mean.
  3. `deal.demo_observations.observed_rent_psf` (v1 staged demo path; preserved unchanged).
  4. `observed_rent_psf: null` with `source: "unavailable"` if nothing resolves — underwriting then skips the rent-driven NOI delta.
- **Malformed CSV** falls back to demo_observations with the failure noted in `provenance` (never silently ignored).
- **Output keys:** `observed_rent_psf` (float | null), `source` (`cli_override` | `comps_csv` | `deal.demo_observations` | `unavailable`), `provenance` (one-line human-readable explanation), `sample_leases[]` (up to 5 records when source == `comps_csv`), `csv_path` (string | null), optional `error` / `message`.

---

## 7. CLI usage

```bash
# Run on the staged demo deal
python agent.py

# Different deal profile
python agent.py --deal deals/<other>.json

# Override observed market rent (otherwise pulled from deal.demo_observations.observed_rent_psf)
python agent.py --observed-rent 68

# Auto-confirm severity-5 checkpoint (skip stdin prompt)
python agent.py --yes

# Smoke-test each tool independently
python tools/violations.py
python tools/market_signals.py
python tools/macro_signals.py     # requires FRED_API_KEY
python tools/underwriting.py
python tools/snapshot_diff.py     # uses synthetic snapshots in a tempdir, no network
python tools/dispatch.py          # v2: 11 boundary cases, no network
python tools/leasing_comps.py     # v2: CSV + demo_observations + override paths

# v2: full hermetic pytest suite (no API keys, no network)
pytest tests/ -v

# Override the model from the env
MODEL_ID=openrouter/anthropic/claude-sonnet-4-5 python agent.py --yes

# Round-trip the LLM (v2: honors MODEL_ID, was hardcoded in v1)
python test_model.py

# Record a demo transcript
./scripts/record_demo.sh
```

---

## 8. What's done

### v1 scope (all on `main`, tagged `v1.0` on `70d0547` and `v1.1` on `782d776`)

- [x] Tool 1: HPD violations (Property)
- [x] Tool 2: ACRIS sales comps (Market)
- [x] Tool 3: FRED 10Y Treasury + SOFR (Macro)
- [x] Tool 4: deterministic NOI/IRR delta math (Underwriting)
- [x] Two-phase LLM (JSON scoring → markdown briefing)
- [x] Severity-5 human checkpoint with stdin prompt and audit log to `runs/`
- [x] `--yes` auto-confirm flag for unattended/recorded runs
- [x] One staged demo deal that hits the killer-quote scenario reliably
- [x] Per-tool standalone smoke tests
- [x] `record_demo.sh` for backup recording
- [x] PRD.pdf checked in
- [x] README with setup, schema, design rationale
- [x] All four PRs merged: violations (#1, #2), market (#3), macro (#4), elliot's misc work (#5)

### Tier 1 (post-v1, on `main`, not yet pushed as of 2026-05-05)

- [x] System prompt extracted to `prompts/system_prompt_v2.txt`, loaded by `agent.py` at module load
- [x] `MODEL_ID` env var support; defaults to current Hy3 string
- [x] `compute_underwriting_delta` echoes `deal_id`, `assumptions_locked_at`, and a `drivers[]` array with per-observed-value `source_tool` provenance
- [x] `tools/snapshot_diff.py` stub wired into `_gather_signals` (later replaced by Tier 2b)
- [x] `.env.example` sanitized to placeholders; `.DS_Store` gitignored + untracked
- [x] Tags `v1.0` / `v1.1` created on the corresponding shas

### Tier 2 (post-v1, on `main`, not yet pushed as of 2026-05-05)

- [x] Atomic snapshot writer (`_write_snapshot`) called inside `_gather_signals` after all four signal tools return
- [x] Snapshots written to `snapshots/{deal_id}/{YYYY-MM-DD}.json` (gitignored), containing only ground-truth tool outputs (NOT the LLM scoring)
- [x] `compare_to_yesterday` real implementation: generic dict walker over `tool_outputs`, surfaces numeric + signal-state categoricals, skips noisy fields
- [x] Snapshot diff rendered into `_signals_block` for phase 1 (three render modes: first-run note, unchanged note, populated JSON)
- [x] `python tools/snapshot_diff.py` smoke test exercises four cases via synthetic snapshots in a tempdir

### v2 (this branch — `feat-jv-v1-final`, complete as of 2026-05-06)

- [x] **`tools/dispatch.py` POST_MATH_DISPATCH band classifier.** Pure Python. Bands on absolute observed IRR (green > 10%, yellow 7.5%–10%, red < 7.5%). Fail-safe red on missing IRR or upstream errors. Boundary-tested.
- [x] **`tools/leasing_comps.py` named tool.** Promotes `demo_observations.observed_rent_psf` into a real dataflow node with CSV upgrade path (`deals/<id>.comps.csv`, SF-weighted) and provenance envelope.
- [x] **`agent._human_checkpoint` rewritten as state machine.** Returns `confirmed | downgrade | abort` (y / d / q). Audit log captures dispatch reason, IRR observed/assumed/delta, NOI delta, drivers, decision, raw input. Re-prompts once on bad input then defaults to abort.
- [x] **`agent._downgrade_dispatch` immutable downgrade.** Red → yellow / advisory. Original snapshot stays as-captured for forensic replay.
- [x] **Synthesis turn replaces phase 1 + phase 2.** Single LLM call. Tone branches via `_EDGE_TONE[edge_label]` (auto_render / advisory / requires_human_review). Model is explicitly forbidden from scoring severity in the system prompt.
- [x] **Compound_finding turn (third LLM call).** Cross-signal reasoning over today's signals + snapshot_diff. Hard guardrails in `prompts/compound_finding.txt`: ≥2 named signals + snapshot_diff anchor per finding, otherwise emits "No compound findings this run."
- [x] **Snapshot now captures `dispatch` and `leasing_comps`** alongside the four signal tools. Generic snapshot_diff walker absorbs them automatically.
- [x] **`agent.py` passes `snapshots_dir=SNAPSHOTS_DIR` to snapshot_diff** so monkeypatch redirection works in tests and bespoke snapshot roots work in production.
- [x] **`_safe_rel(path)` defensive path formatting** so logs don't crash when paths live outside REPO_ROOT (test tmp dirs, custom snapshot roots).
- [x] **`prompts/system_prompt_v2.txt` rewritten** for synthesis-only role. Severity scoring rules removed; dispatch's load-bearing role documented.
- [x] **`prompts/compound_finding.txt` added** with empty-state line and hard cite-≥2-signals constraint.
- [x] **`tests/` hermetic pytest suite (58 tests).** `conftest.py` stubs strands, fakes requests, redirects runs/snapshots to tmp_path, and exposes a `demo_deal` fixture. Suite runs in &lt;1s with no API keys. Coverage: dispatch boundaries (18), leasing_comps fallback chain (8), checkpoint state machine + audit log (25), end-to-end run loop with all three bands and all three checkpoint outcomes (6).
- [x] **`test_model.py` honors `MODEL_ID`** (was hardcoded to Hy3 in v1) so the smoke test stays in parity with `agent.py`.
- [x] **`.env.example` documents the Claude swap.** Two commented options (OpenRouter gateway or direct Anthropic) so flipping the brain is a one-line change.
- [x] **README and CODEBASE_AUDIT refreshed.** v2 architecture, repo layout, design decisions, known model behaviors all updated.

### Branches not yet on main

The only branch with work *not* yet on main is `feature/real-data-sources` (last commit 2026-05-02). It contains a Documenso-flavored demo UI with SSE streaming, a v2 UI design spec, callback-style events, and a phase-1 JSON repair-retry. **This is unmerged and not part of v1 or v2.** Decision on whether to merge or rebuild is open.

---

## 9. What's NOT built (v3 scope)

- ~~Daily snapshot + diff: "what changed since yesterday"~~ → **shipped in Tier 2**
- ~~POST_MATH_DISPATCH band classifier~~ → **shipped in v2**
- ~~Three-outcome human checkpoint (confirm / downgrade / abort)~~ → **shipped in v2**
- ~~Math-only materiality (model no longer scores severity)~~ → **shipped in v2**
- ~~Compound_finding cross-signal reasoning~~ → **shipped in v2**
- ~~Leasing comps as named tool~~ → **shipped in v2**
- ~~Hermetic test suite~~ → **shipped in v2**
- ~~Claude model swap via env~~ → **configured in v2; live verification deferred to user**
- Memory across runs: agent remembers which alerts the analyst confirmed vs. dismissed (override-log injection into the synthesis turn)
- Multi-step reasoning across signal buckets ("rates moved AND a comp traded — together that means…") via a `compound_finding` output field
- Real planner: agent decides which sources to check for *this* deal, not all of them every run
- Model swap to Claude (the `MODEL_ID` env var plumbing is in place; the actual swap and the side-by-side recording are not)
- Slack/email delivery instead of stdout
- Tenant credit watch (SEC EDGAR)

Not in v2 either, but worth flagging as gaps:
- **Multi-deal/portfolio support.** The repo has one deal JSON; nothing iterates over a folder.
- **Tests beyond smoke tests.** No pytest, no CI, no coverage. Each tool's `__main__` is the only suite.
- **Caching.** NYC Open Data has no auth but rate-limits exist — re-running hits the APIs every time.
- **No web UI.** Output is markdown to stdout. The unmerged `feature/real-data-sources` branch attempts a UI but is not on main.
- **Live leasing-comps feed.** The "observed market rent" is currently staged in `demo_observations`. Production would need CompStak or equivalent.
- **Underwriting math validation.** No second-source check that `_irr()` and `_build_cash_flows()` match an Excel model side-by-side.
- **Snapshot retention policy.** Snapshots accumulate forever under `snapshots/{deal_id}/`; no pruning. Open question whether to prune at 90 days or keep indefinitely.

---

## 10. Known model behaviors (already documented in README)

- **Hy3 leaks reasoning text** unless `extra_body.reasoning.exclude=True`. Reasoning tokens count against `max_tokens`, so it must be ≥ 8192 (currently 16384).
- **`reasoningContent is not supported in multi-turn`** is a harmless LiteLLM warning.
- **Hy3's knowledge cutoff predates today.** System prompt explicitly states the current date so it doesn't flag current data as future-dated. (Note: the agent does not currently inject the current date — see `prompts/system_prompt_v2.txt` (Tier 1a moved this off `agent.py`). This is a small gap vs. what the README claims.)
- **NYC Open Data** has no auth but rate-limits exist. Caching is deferred to v2.

---

## 11. Recent commit timeline (newest first)

```
2026-05-05  aa42536  feat(agent): render snapshot diff into _signals_block for phase 1   [Tier 2c]
2026-05-05  6beea39  feat(snapshot_diff): replace stub with real day-over-day diff       [Tier 2b]
2026-05-05  9a31272  feat(agent): add atomic snapshot writer + reorder _gather_signals   [Tier 2a]
2026-05-05  56d82fd  feat(agent): wire snapshot_diff stub between signal tools and uw    [Tier 1d]
2026-05-05  be07fed  feat(underwriting): echo deal_id, assumptions_locked_at, drivers    [Tier 1c]
2026-05-05  285dc37  feat(agent): support MODEL_ID env var, default Hy3                  [Tier 1b]
2026-05-05  2168870  refactor(agent): extract system prompt to prompts/system_prompt_v2.txt  [Tier 1a]
2026-05-05  cb0659e  chore: gitignore .DS_Store and remove tracked copy
2026-05-05  59c4645  chore: sanitize .env.example to placeholder values
2026-05-01  adb420d  Merge PR #5 from JoshuaViera/elliot                                  [v1.1, tagged]
2026-04-30  782d776  v1.1 + Fully Tested = Done                                           [v1.1, tagged]
2026-04-30  70d0547  v1 = done                                                            [v1.0, tagged]
2026-04-29  4425753  Merge PR #4 (macro signals)
2026-04-29  ccfaedf  replaced Exposed Key
2026-04-29  ff5d8bd  Tool 3: Treasury 10Y macro signals + agent wiring
2026-04-29  ff599af  Merge PR #3 (market signals)
2026-04-29  50b8fb9  fix: enforce borough filter on ACRIS Legals join + strengthen tests
2026-04-28  e3593a0  Merge PR #2 (violations)
2026-04-28  e63e4fe  feat: market signals tool (ACRIS DEED/DEEDO comps)
2026-04-27  e81f59e  feat(violations): NYC HPD violations tool wired into agent loop
2026-04-27  f7ac847  Milestone 1: Hy3 model connection working
```

Tags: `v1.0` (on `70d0547`), `v1.1` (on `782d776`). Local only — not yet pushed.

Unmerged branch: `feature/real-data-sources` (UI work, last commit 2026-05-02).

The 9 commits above `adb420d` are local on `main` and have NOT been pushed to origin yet — Joshua wants to confirm a green `python agent.py --yes` end-to-end run before pushing.

---

## 12. Team

| Person            | Role                                                  |
| ----------------- | ----------------------------------------------------- |
| Joshua Viera      | Engineering lead, integration owner, final audit      |
| Pedro Martins     | Tool 2 — Market signals (ACRIS)                       |
| Elliot Chen       | Tool 3 — Macro signals (FRED), demo slide advancement |
| Kevin Natera      | Slides, integration QA                                |
| Gamaliel Leguista | Merges, integration QA, presenting                    |

---

## 13. What I'm asking you (the next Claude) to do

You now have the full state of the project. The user just had a conversation with you (or a sibling Claude) about **next steps**, but that conversation lacked this context. With this snapshot in hand, please produce a markdown plan that:

1. Lists concrete next steps in priority order, distinguishing what should ship before the demo from what is post-demo polish.
2. For each step, specifies: which file(s) change, what the acceptance check looks like, and roughly how big it is.
3. Calls out any decision the user has to make (e.g., merge `feature/real-data-sources` vs. rebuild that UI on main; which v2 item to start first; whether to add pytest/CI).
4. Flags risks specific to the demo (Hy3 free-tier rate limits, ACRIS slowness, FRED key absence, severity-5 prompt during a recorded run, etc.).

The user will hand the resulting markdown back to me and I'll execute against it.
