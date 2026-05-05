"""
Demo-MVP web UI for CRE Deal Pulse.

Spawns `python agent.py --yes` and streams its stdout to the browser as
Server-Sent Events. The full v2 design (in-process callback, checkpoint
pause/resume, comps CRUD, pipeline view) lives in
docs/superpowers/specs/2026-05-01-deal-pulse-ui-design.md and is deferred.

Run:
    uvicorn web.server:app --reload
    open http://localhost:8000

This is intentionally narrow: one staged deal, one live ticker, one briefing
render. No deal CRUD, no comps editor, no severity-5 pause — uses --yes so
the agent auto-confirms while the UI surfaces the checkpoint reasoning as a
banner for the demo narrative.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = Path(__file__).resolve().parent / "index.html"
DEFAULT_DEAL = REPO_ROOT / "deals" / "midtown-south-office-001.json"

app = FastAPI(title="Deal Pulse — demo")


# ─────────────────────────────────────────────────────────────────────────
# Static surface
# ─────────────────────────────────────────────────────────────────────────

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/api/deal")
async def deal() -> dict:
    """Return the staged deal so the UI can render the header without a scan."""
    if not DEFAULT_DEAL.exists():
        raise HTTPException(status_code=404, detail="staged deal not found")
    with open(DEFAULT_DEAL) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────
# Live scan via SSE
# ─────────────────────────────────────────────────────────────────────────
# We run agent.py as a subprocess and parse its stdout into structured SSE
# events. The agent's print format is stable enough for this — the v2 path
# replaces the parser with an in-process callback (see spec §1).
#
# Subprocess uses Popen with an argv LIST (no shell=True), so user-controlled
# query params cannot inject shell commands.

_TOOL_LINE = re.compile(r"^\s*\[tool\]\s+(.+?)\s+…\s*$")
_RETRY_LINE = re.compile(r"^\s*\[retry\]\s+(.+?)\s*…?\s*$")

_TOOL_ID_BY_LABEL = {
    "HPD violations on target BBL": "hpd",
    "Lease comps rent signal": "leasing",
    "FRED 10Y Treasury + SOFR": "fred",
    "Underwriting delta (deterministic Python)": "underwriting",
}


def _classify_tool_label(label: str) -> str:
    """Map an agent stdout label to a stable tool id for the UI ticker."""
    if label.startswith("ACRIS recent sales"):
        return "acris"
    return _TOOL_ID_BY_LABEL.get(label, "unknown")


def _spawn_agent(deal_path: Path) -> subprocess.Popen:
    """Start the agent as a child process with argv list (no shell)."""
    return subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "agent.py"), "--deal", str(deal_path), "--yes"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )


def _reader_thread(proc: subprocess.Popen, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """Background thread: ship each agent stdout line into the asyncio queue."""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            asyncio.run_coroutine_threadsafe(queue.put(line.rstrip("\n")), loop)
    finally:
        proc.wait()
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)


async def _stream_agent(deal_path: Path) -> AsyncIterator[dict]:
    """Run agent.py as a subprocess and yield structured SSE events.

    Event shapes:
        {event: "tool_started",  data: {tool, label}}
        {event: "phase",          data: {name}}     # scoring | drafting
        {event: "retry",          data: {message}}
        {event: "checkpoint",     data: {raw}}
        {event: "briefing_chunk", data: {markdown}}
        {event: "complete",       data: {report_path?}}
        {event: "error",          data: {message}}
    """
    proc = _spawn_agent(deal_path)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=_reader_thread, args=(proc, queue, loop), daemon=True).start()

    in_briefing = False
    in_checkpoint = False
    briefing_started = False
    checkpoint_lines: list[str] = []

    try:
        while True:
            line = await queue.get()
            if line is None:
                break

            # Briefing capture: starts after the DEAL PULSE BRIEFING banner
            # and ends at the "Saved report to …" line.
            if line.strip() == "DEAL PULSE BRIEFING":
                in_briefing = True
                continue
            if in_briefing and line.startswith("Saved report to "):
                yield {
                    "event": "complete",
                    "data": json.dumps({
                        "report_path": line.replace("Saved report to ", "").strip(),
                    }),
                }
                in_briefing = False
                continue
            if in_briefing:
                # Skip the '=' rule lines that wrap the briefing banner
                if set(line.strip()) == {"="}:
                    continue
                # Skip the leading blank line right after the banner
                if not briefing_started and not line.strip():
                    continue
                briefing_started = True
                yield {
                    "event": "briefing_chunk",
                    "data": json.dumps({"markdown": line + "\n"}),
                }
                continue

            # Checkpoint block: capture between the two '=' rule rows
            if line.strip().startswith("CHECKPOINT — severity-5"):
                in_checkpoint = True
                checkpoint_lines = []
                continue
            if in_checkpoint:
                if line.startswith("--yes flag set") or line.startswith("Decision logged to"):
                    in_checkpoint = False
                    yield {
                        "event": "checkpoint",
                        "data": json.dumps({"raw": "\n".join(checkpoint_lines).strip()}),
                    }
                    continue
                checkpoint_lines.append(line)
                continue

            tool_match = _TOOL_LINE.match(line)
            if tool_match:
                label = tool_match.group(1)
                yield {
                    "event": "tool_started",
                    "data": json.dumps({
                        "tool": _classify_tool_label(label),
                        "label": label,
                    }),
                }
                continue

            retry_match = _RETRY_LINE.match(line)
            if retry_match:
                yield {
                    "event": "retry",
                    "data": json.dumps({"message": retry_match.group(1)}),
                }
                continue

            stripped = line.strip()
            if stripped == "Scoring with model…":
                yield {"event": "phase", "data": json.dumps({"name": "scoring"})}
                continue
            if stripped == "Drafting briefing…":
                yield {"event": "phase", "data": json.dumps({"name": "drafting"})}
                continue
            if stripped.startswith("[ERROR]"):
                yield {"event": "error", "data": json.dumps({"message": stripped})}
                continue

        if proc.returncode not in (0, None):
            yield {
                "event": "error",
                "data": json.dumps({"message": f"agent exited with code {proc.returncode}"}),
            }
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


@app.get("/api/scan/stream")
async def scan_stream(deal: Optional[str] = None) -> EventSourceResponse:
    deal_path = Path(deal) if deal else DEFAULT_DEAL
    if not deal_path.is_absolute():
        deal_path = REPO_ROOT / deal_path
    if not deal_path.exists():
        raise HTTPException(status_code=404, detail=f"deal not found: {deal_path}")
    return EventSourceResponse(_stream_agent(deal_path))
