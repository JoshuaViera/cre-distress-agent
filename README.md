# CRE Deal Pulse

An AI agent that monitors live market, property, and macro signals against a CRE analyst's deal underwriting and tells them — in plain English — when reality is breaking their assumptions and what to do about it.

> _"Market rent in Midtown South dropped to $68/SF this week. Your deal assumes $74. At $68, your NOI falls by $180K and your IRR drops from 14% to 11%. Recommend re-running underwriting before proceeding to LOI."_

That's the product. The analyst didn't have to check CoStar, read three articles, or update their model. The agent did it and told them what it means.

---

## Why this exists

A junior CRE acquisitions analyst spends hours every day refreshing the same sources — CoStar, FRED, news feeds, NYC city portals — to check whether the deals already in their pipeline still pencil. Rates moved, a comp traded, a violation hit the target property: any one signal can change the underwriting. The integration happens in the analyst's head, across five tabs, and material changes get caught late.

Deal Pulse replaces "five tabs and a spreadsheet" with a one-page briefing. It loads the analyst's deal assumptions, scans three buckets of public data, computes the dollar and IRR impact deterministically in Python, and produces a markdown briefing scored by materiality.

Built for the Pursuit AI-Native Fellowship Cycle 3.

---

## How it works

**Input:** a deal profile JSON (address, BBL, asset class, underwriting assumptions).

**Three signal buckets:**

| Bucket   | Source                 | What it watches                                              |
| -------- | ---------------------- | ------------------------------------------------------------ |
| Property | NYC HPD violations API | Open code violations, severity breakdown on the target asset |
| Market   | NYC ACRIS              | Recent comparable sales by borough and submarket             |
| Macro    | FRED                   | 10-year Treasury, SOFR                                       |

**The loop:**

1. Load the deal profile.
2. Call each signal tool.
3. Pass observed values + assumed values to a deterministic Python function (`compute_underwriting_delta`) that calculates NOI and IRR impact. _Math runs in code, not in the model._
4. The LLM scores materiality 1–5 and narrates the impact in analyst-style language.
5. If any signal scores 5, the agent **pauses for human review** before producing the final briefing.
6. Output: a markdown briefing — top alerts first, full change log below.

**Stack:** Strands (agent framework) · LiteLLM (provider adapter) · OpenRouter (gateway) · Tencent Hy3 Preview (v1 model, free tier) · Python 3.13.

---

## Quick start

**Prereqs:** Python 3.13, an OpenRouter API key, a free FRED API key.

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
```

Run the agent on the staged demo deal:

```bash
python agent.py
```

You should see the agent call HPD, ACRIS, and FRED in sequence, score each signal, pause at any severity-5 alert, and emit a markdown briefing to stdout.

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

---

## Repo layout

```
cre-distress-agent/
├── agent.py                  # Main agent loop, system prompt, tool wiring
├── tools/
│   ├── violations.py         # Tool 1: HPD violations (Property signals)
│   ├── market_signals.py     # Tool 2: ACRIS sales (Market signals)
│   ├── macro_signals.py      # Tool 3: FRED rates (Macro signals)
│   └── underwriting.py       # Deterministic NOI/IRR delta math
├── deals/
│   └── midtown-south-office-001.json  # Staged demo deal
├── test_model.py             # Hy3 round-trip smoke test
├── requirements.txt
├── .env.example
└── README.md
```

---

## v1 scope

- Three signal tools: HPD, ACRIS, FRED
- Deterministic underwriting delta math in Python
- Materiality scoring (1–5) by the LLM
- Human checkpoint on severity-5 alerts
- Markdown briefing to stdout
- Single pre-staged demo deal

## v2 scope

- Daily snapshot + diff: _"what changed since yesterday"_
- Memory across runs: the agent remembers which alerts the analyst confirmed vs. dismissed
- Multi-step reasoning across signal buckets: _"rates moved AND a comp traded — together that means…"_
- Real planner: agent decides which sources to check for this deal, not all of them every run
- Slack/email delivery instead of stdout
- Tenant credit watch (SEC EDGAR)

The v1 → v2 story is the pitch for what a frontier model unlocks.

---

## Design decisions worth flagging

**Math is deterministic, not LLM-vibes.** The NOI and IRR delta runs in Python. The model only narrates the result. _"We compute the impact deterministically and the agent narrates"_ is a stronger story than _"the LLM did some math, hopefully right."_

**Materiality scoring is LLM, not rules.** The model scores 1–5 based on the _computed delta_, not the raw signal. A 25 bps move on a deal with 30% LTV matters less than the same move on an 80% LTV deal — that judgment is what the model is for.

**Human checkpoint is a feature, not a limitation.** Severity-5 alerts pause the agent and surface its reasoning. The cohort brief requires it; the product needs it; partners shouldn't get auto-pinged.

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

- **Hy3 leaks reasoning text.** It's a reasoning model; `max_tokens` must be ≥ 8192 to avoid mid-thought truncation. LiteLLM logging is suppressed for clean demo output.
- **`reasoningContent is not supported in multi-turn`** is a harmless LiteLLM warning. Ignore.
- **Hy3 sometimes thinks it's 2024.** Its knowledge cutoff predates today. The system prompt explicitly tells it the current date so it doesn't flag current data as future-dated.
- **NYC Open Data has no auth** but rate-limits exist. Caching is a v2 concern.

---

## License

MIT — fellowship project, repo is public for demo purposes.
