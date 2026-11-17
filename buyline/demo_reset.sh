#!/bin/sh
# Clean start for recording: clears this run's outputs, keeps Cognee's memory of past orders (data/) and prerun.json.
cd "$(dirname "$0")" || exit 1
rm -f state.json orders.json briefing.mp3 fo_log.jsonl
echo "reset: state, orders, briefing, Fo log cleared. Kept: Cognee memory (data/), prerun.json $([ -f prerun.json ] && echo present || echo absent)."
