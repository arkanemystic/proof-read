#!/usr/bin/env bash
# Restart loop for the overnight build. Survives session exits; logs each attempt.
cd ~/work/proofread
unset ANTHROPIC_API_KEY
while [ ! -f DONE ]; do
  echo "=== attempt $(date -u +%FT%TZ) ===" | tee -a run.log
  claude -p "$(cat PROMPT.md)" --dangerously-skip-permissions 2>&1 | tee -a run.log
  echo "=== session exited $(date -u +%FT%TZ), retrying in 10 min ===" | tee -a run.log
  sleep 600
done
echo "DONE found, loop finished." | tee -a run.log
