#!/usr/bin/env bash
set -euo pipefail
cd ~/work/proofread
[ -f PROMPT.md ] || { echo "PROMPT.md missing"; exit 1; }
[ -f .env ] || { echo ".env missing, run ./setup-secrets.sh"; exit 1; }
if grep -qE '\{[A-Z_]+\}' PROMPT.md; then echo "Unfilled placeholders:"; grep -oE '\{[A-Z_]+\}' PROMPT.md | sort -u; exit 1; fi
unset ANTHROPIC_API_KEY
tmux new-session -d -s proofread 'cd ~/work/proofread && unset ANTHROPIC_API_KEY && until [ -f DONE ]; do claude -p "$(cat PROMPT.md)" --dangerously-skip-permissions 2>&1 | tee -a run.log; sleep 600; done'
echo "Started. Attach with: tmux attach -t proofread   (detach: Ctrl-b d)"
