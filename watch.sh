#!/usr/bin/env bash
# Read-only progress view. Safe to Ctrl-c; it does not touch the run.
cd ~/work/proofread
while true; do
  clear
  echo "== $(date -u +%FT%TZ)   (Ctrl-c exits this viewer only)"
  echo; echo "-- recent commits"; git log --oneline -8 2>/dev/null
  echo; echo "-- PROGRESS.md (tail)"; tail -15 PROGRESS.md 2>/dev/null || echo "(not yet created)"
  echo; echo "-- last agent messages"
  tail -200 run.stream.jsonl 2>/dev/null | jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' 2>/dev/null | tail -8 | cut -c1-200
  echo; echo "-- last tool calls"
  tail -200 run.stream.jsonl 2>/dev/null | jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="tool_use") | "\(.name): \(.input | tostring | .[0:120])"' 2>/dev/null | tail -6
  sleep 20
done
