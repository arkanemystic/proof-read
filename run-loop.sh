#!/usr/bin/env bash
# Restart loop for the overnight build. Ignores Ctrl-C; streams progress to run.log.
trap '' INT
cd ~/work/proofread
unset ANTHROPIC_API_KEY
while [ ! -f DONE ]; do
  echo "=== attempt $(date -u +%FT%TZ) ===" | tee -a run.log
  claude -p "$(cat PROMPT.md)" --dangerously-skip-permissions --output-format stream-json --verbose >> run.stream.jsonl 2>> run.log
  echo "=== session exited $(date -u +%FT%TZ), retrying in 1 min ===" | tee -a run.log
  sleep 60
done
echo "DONE found, loop finished." | tee -a run.log
