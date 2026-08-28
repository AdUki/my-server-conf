#!/usr/bin/env bash
# Drain the device-facts queue in bulk. Each --warm-facts run resolves a
# batch and stops early if the provider is busy, so this loops with a pause
# between batches rather than hammering a shared rate limit.
set -uo pipefail
SCRIPT_DIR="$(realpath "$(dirname "$0")")"
ROUNDS="${1:-40}"
for i in $(seq 1 "$ROUNDS"); do
    left=$(python3 -c "import json;print(len(json.load(open('$HOME/.ai-marketplace-monitor/device_facts_pending.json'))))" 2>/dev/null || echo 0)
    [ "$left" -eq 0 ] && { echo "queue empty after $((i-1)) rounds"; break; }
    echo "round $i: $left queued"
    "$SCRIPT_DIR/run.sh" --warm-facts 2>&1 | grep -E "^\s+\S|Resolved" | tail -12
    sleep 45
done
