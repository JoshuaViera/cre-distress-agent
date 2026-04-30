#!/usr/bin/env bash
# Capture a clean transcript of an end-to-end Deal Pulse run for the demo
# backup recording. Use this Friday during rehearsal so the "backup pivot"
# in the PRD demo script has a real artifact to fall back on.
#
# Usage:
#   ./scripts/record_demo.sh [path/to/deal.json]
#
# Output:
#   demo_recording_<timestamp>.txt  — full terminal transcript
#
# Pair with a screen recording (macOS: Cmd-Shift-5) of the same run so the
# "Kevin/Elliot switches to the pre-recorded run" step has video, not just text.

set -euo pipefail

cd "$(dirname "$0")/.."

DEAL="${1:-deals/midtown-south-office-001.json}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="demo_recording_${TS}.txt"

if [ ! -f .env ]; then
  echo "ERROR: .env missing. Copy .env.example and fill in OPENROUTER_API_KEY + FRED_API_KEY."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "ERROR: .venv missing. Run: python -m venv .venv && pip install -r requirements.txt"
  exit 1
fi

echo "Recording demo run on deal: $DEAL"
echo "Transcript: $OUT"
echo "Press y at the checkpoint to confirm the briefing."
echo "---"

# `script` records the full terminal session including agent stdin/stdout.
# Flags differ between Linux and macOS; -q quiet, output file as last arg.
if [[ "$(uname)" == "Darwin" ]]; then
  script -q "$OUT" .venv/bin/python agent.py --deal "$DEAL"
else
  script -q -c ".venv/bin/python agent.py --deal $DEAL" "$OUT"
fi

echo "---"
echo "Saved transcript to $OUT"
echo "Now capture a screen recording (Cmd-Shift-5 on macOS) of an identical run for the demo backup."
