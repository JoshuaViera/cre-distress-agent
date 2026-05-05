# CRE Deal Pulse — UI Design (v2)

**Status:** design only · implementation deferred to post-cycle
**Date:** 2026-05-01
**Branch context:** `feature/real-data-sources`

## Why this is a design doc, not a build plan

The v1 PRD explicitly de-scopes a UI; v1 ships with a markdown briefing to stdout. This document captures the brainstormed shape of the UI as the headline v1→v2 upgrade, paired with the planned model swap (Hy3 → Claude). It is meant to be referenced in the demo deck and revisited after the fellowship cycle, not implemented before Saturday.

Two small fixes from this brainstorm are landing immediately and benefit the CLI as well as any future UI:

1. `_step` → `on_event` callback in `agent.py` — agent core stays neutral about how events are reported.
2. Phase-1 JSON repair-retry — guards the demo against a single malformed Hy3 response.

Everything else below is the v2 plan.

---

## §1 — Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Browser (vanilla TS, no framework)                                  │
│  /                /deal/{id}              /deal/{id}/comps           │
│  pipeline table   live scan + briefing    Edit · Import · Generate   │
│         REST                EventSource (SSE)         REST           │
└─────────┼─────────────────────┼────────────────────────┼─────────────┘
          │                     │                        │
┌─────────┴─────────────────────┴────────────────────────┴─────────────┐
│  FastAPI app                                                         │
│   /api/deals · /api/scans · /api/comps                               │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ imports run_scan_phase1/2
┌─────────────────────────────────┴────────────────────────────────────┐
│  agent core (existing modules, two refactors)                        │
│   • run_scan_phase1(deal, on_event) → signals, scoring, high_sev      │
│   • run_scan_phase2(deal, signals, scoring, decision) → briefing      │
│   • tools/ unchanged                                                 │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
       deals/*.json   data/comps/*.csv   runs/*.json + *.md
```

Three layers, narrow seams. The frontend never touches the filesystem; the backend never imports HTTP into the agent. The agent's existing phase split (`_phase1_query` / `_human_checkpoint` / `_phase2_query`) maps 1:1 onto SSE events.

### Refactors to existing code

1. **Callback for tool events.** `_step(label)` → `on_event(kind, payload)`. CLI passes a `print`-wrapper; API passes an SSE pump. ~40 lines.
2. **Phase split.** Expose `run_scan_phase1()` and `run_scan_phase2()` as importable functions. The `_human_checkpoint` stdin prompt moves out of `run()` and into the CLI entry point; the API replaces it with an awaitable Future resolved by `POST /scans/{run_id}/decision`.

The CLI keeps working unchanged.

---

## §2 — Data layout

```
deals/<deal_id>.json                  deal profile (existing schema)
data/comps/<deal_id>.csv              per-deal lease comps (was a shared sample)
runs/<run_id>.json                    full run artifact (existing)
runs/<run_id>.md                      briefing (existing)
runs/checkpoints/<run_id>.json        pending severity-5 decisions (new)
runs/<ts>.log                         checkpoint audit logs (existing)
```

`run_id` = `<UTC-timestamp>-<deal_id>` — already the convention in `agent._write_run_artifacts`. Per-deal CSVs are referenced via `deal.data_sources.lease_comps_csv`; that field already exists in the deal schema.

The persistence boundary is unchanged from v1. The UI reads exactly what the CLI already writes.

---

## §3 — REST surface

```
GET    /api/deals                              list + last-run summary
GET    /api/deals/{id}                         detail
GET    /api/deals/{id}/runs                    history
GET    /api/deals/{id}/runs/{run_id}           full run artifact
POST   /api/deals/{id}/scans                   start scan; returns {run_id}
GET    /api/scans/{run_id}/stream              SSE
POST   /api/scans/{run_id}/decision            {decision: "confirm"|"override"}
GET    /api/deals/{id}/comps                   list rows
POST   /api/deals/{id}/comps                   add row
PUT    /api/deals/{id}/comps/{row_id}          edit row
DELETE /api/deals/{id}/comps/{row_id}          delete row
POST   /api/deals/{id}/comps/import            paste/upload + validate
POST   /api/deals/{id}/comps/generate          synthetic, labeled in source col
```

One in-flight scan per deal — `POST /scans` returns 409 if one is already running for the target deal.

---

## §4 — SSE event schema

Each event has an `event:` line and a JSON `data:` payload.

```
event: tool_started        {tool, label, ts}
event: tool_completed      {tool, output_summary, source_url, ts}
event: tool_failed         {tool, error, ts}
event: scoring_started     {ts}
event: scoring_completed   {scoring: [...], ts}
event: checkpoint_required {findings: [...], ts}        ← stream pauses
event: checkpoint_resolved {decision, ts}
event: briefing_started    {ts}
event: complete            {run_id, briefing_path}
event: error               {stage, message}
```

`tool` is one of `hpd | leasing | acris | fred | underwriting`. `output_summary` is a one-liner the UI can drop into the ticker without parsing the full tool output.

Reconnects send `Last-Event-ID`; the server replays from a small per-run ring buffer (held alongside the `RunSession`). Buffer evicts on `complete` or `error`.

---

## §5 — Live scan flow & checkpoint coordination

```
POST /scans
  └─ spawn asyncio task → run_scan_phase1(deal, on_event=sse_emitter)
        ├─ for each of 5 tools: emit tool_started → call tool → emit tool_completed
        ├─ emit scoring_started → LLM phase 1 → emit scoring_completed
        └─ if any severity-5:
                emit checkpoint_required
                write runs/checkpoints/<run_id>.json
                await asyncio.Future stored in RunSession[run_id]

POST /scans/{run_id}/decision
  └─ resolve the parked Future with the user's decision
        └─ run_scan_phase2 resumes inside the original task:
                emit briefing_started → LLM phase 2 → _write_run_artifacts → emit complete
```

State store is an in-process `dict[run_id, RunSession]` keyed by `run_id`. Each `RunSession` holds: the asyncio task handle, the parked Future (or None if not waiting), and the SSE event ring buffer. Single-worker uvicorn keeps this trivial; multi-worker would require Redis or sticky sessions and is explicitly out of scope.

If the UI navigates away while a checkpoint is pending, the run remains parked in `runs/checkpoints/<run_id>.json` and shows up as "awaiting decision" on the deal page on next load.

---

## §6 — Frontend pages (vanilla TS, modernist typography)

Visual direction: black on near-white background, single accent color for severity-5, two type weights (300 / 700), tracked uppercase labels at 10 px / 0.18 em, generous line-height, big light-weight hero numbers. No framework, no client-side store, no bundler — `tsc` compiles to ES2020 and FastAPI serves the static output.

### `/` — pipeline table

One row per deal. Columns: deal name, stage, observed IRR, IRR delta vs assumed, max-severity badge, last-scan timestamp. Sortable on every column. Click a row → `/deal/{id}`. A thin sev-3+ alert strip pinned above the table catches the eye even before sorting.

### `/deal/{id}` — live scan + briefing

The visible spine of the product. Layout (already mocked in `.superpowers/brainstorm/.../deal-detail-modernist.html`):

- **Header.** Address as a 36 px / weight 300 hero number; tracked-uppercase metadata (submarket, asset class, SF, deal stage); IRR observed-vs-assumed in tabular figures, accent-colored when negative.
- **Left column — live process ticker.** Monospaced lines: `<timestamp> · <TOOL> · <status> · <one-line summary>`. Tools fill in as `tool_completed` events arrive. The model's scoring step shows a "thinking" pulse. Severity-5 surfaces inline as a black-on-white confirm/override pair.
- **Right column — signal cards.** One card per signal — HPD (property), Leasing and ACRIS (both market), FRED (macro), Underwriting — five cards total. Each shows observed-vs-assumed and the severity score. Cards highlight when their severity ≥ 4.
- **Right column footer — lease-comps strip.** The CSV that produced the rent signal, rendered as a small table with a `+ Add comp` affordance. Linked to `/deal/{id}/comps` for full editing.

### `/deal/{id}/comps` — CSV management

Three tabs over a shared underlying CSV file at `data/comps/<deal_id>.csv`:

- **Edit.** Inline row-by-row table editor. Save commits the whole file atomically (write tmp + rename).
- **Import.** Paste rows or upload a CSV. Backend validates against `tools.leasing_signals.REQUIRED_COLUMNS`, shows a diff preview (additions / changes / removed), user picks merge or replace.
- **Generate.** Form: target observed rent ($/SF), comp count (3–10), date range (default last 60 days), optional submarket override. Backend synthesizes plausible rows with addresses sampled from a small pool of nearby streets and rents jittered around the target. Every generated row carries `source = "synthetic"` so a real run can never accidentally treat fake data as real.

### `/deal/{id}/runs/{run_id}` — historical run viewer

Same layout as the live page, but everything replays from `runs/<run_id>.json`. No SSE, no buttons. Used for sharing finished runs (the link works as a static permalink).

---

## §7 — CSV inputs in detail

All three modes write `data/comps/<deal_id>.csv` with the column contract already enforced by `tools/leasing_signals.py`: `address, submarket, asset_class, lease_date, rent_psf, square_feet, source` (plus an optional `tenant`).

| Mode | UX | Validation | Source-column value |
|---|---|---|---|
| CRUD | inline table editor | per-cell on blur | preserved on edit; new rows default to `"manual"` |
| Import | paste or upload | full-file dry-run, diff preview | preserved from imported rows |
| Generate | parameter form | constraint check on inputs | `"synthetic"` always |

Synthetic mode is the demo-safety knob: a presenter can dial in the killer-quote scenario (e.g., target rent $68 with 3 comps) without editing files on disk live on stage.

---

## §8 — Error handling

| Failure | Behavior |
|---|---|
| Tool returns error envelope | emit `tool_failed`; category scored 1; briefing marks "unavailable". Matches the existing tool contract. |
| Phase-1 JSON malformed | retry once with a repair prompt ("Output JSON only — your previous response had prose around it"); if still bad, emit `error` and abort the run. **Lives in agent core, so the CLI gets it too.** |
| Tool exception (uncaught) | emit `error{stage: "tool/<name>"}`; close SSE; surface in UI with retry CTA. |
| Mid-scan crash | emit `error{stage: <last>}`; close SSE; deal page shows "Scan failed at <stage>" with retry. |
| SSE disconnect | UI auto-reconnects with `Last-Event-ID`; backend replays from ring buffer. |
| Concurrent scan on same deal | 409 Conflict; one in-flight scan per deal. |
| Stale checkpoint (UI gone) | run parks in `runs/checkpoints/<run_id>.json`; deal page shows "awaiting decision" on reload. |

---

## §9 — Testing

- **Backend.** pytest with `run_scan_phase1` / `run_scan_phase2` mocked to emit canned event sequences. Assert SSE pump serializes events correctly and `/decision` resumes the parked Future.
- **Frontend.** Playwright smoke test against a recorded fixture run; confirms ticker fills in order, signal cards highlight on sev ≥ 4, severity-5 confirm/override paths both reach `complete`.
- **Existing tool tests.** `python tools/<x>.py` self-tests are unchanged and continue to gate per-tool behavior.
- **Replay mode.** The API exposes a switch to replay a saved run from `runs/<run_id>.json` instead of calling the LLM. Useful for offline demos and CI without burning OpenRouter quota.

---

## §10 — Out of scope (explicit)

- Auth / multi-user / RBAC
- Multi-worker deployment (single-worker uvicorn is the deployment story)
- Daily cron / background scheduling (the `POST /scans` endpoint is the trigger; an external cron just hits it)
- Real-time alerts via Slack/email (already deferred in the v1 PRD)
- Mobile-first responsive tuning beyond "doesn't crash on a phone"
- Cross-deal aggregate dashboard (deferred; pipeline table covers the v2 use case)

---

## §11 — Open questions for v2 implementation

- Should `runs/checkpoints/<run_id>.json` survive a server restart and resurface the parked checkpoint on next boot, or should an interrupted run be marked failed and re-runnable? (Bias: mark failed; the parked Future is in-process and can't be safely resumed across restarts.)
- Synthetic-comp address pool: hand-curated list per submarket, or a small static dataset shipped in the repo? (Bias: ship a static `data/synthetic/streets-by-submarket.json` so generation is deterministic and reviewable.)
- Run-history retention: keep all runs forever, or archive runs older than N days? (Bias: keep forever; runs are small JSON + Markdown and the audit trail is the point of the product.)

---

## §12 — Immediate follow-ups (to land before Saturday demo)

1. **JSON repair-retry** in `agent.py` — one retry with a "Output JSON only" repair prompt around the `_extract_json` failure path. Higher demo-leverage than any UI work.
2. **`_step` → `on_event` callback** — sets up v2 cleanly without breaking the CLI, and is a stronger story to point at in the v1→v2 slide than abstract "we'll add memory."

These two ship now. Everything else in this document waits for v2.
