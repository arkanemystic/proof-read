#!/usr/bin/env bash
set -euo pipefail
cd ~/work/proofread
[ -f PROMPT.md ] || { echo "PROMPT.md missing"; exit 1; }
[ -f .env ] || { echo ".env missing, run ./setup-secrets.sh"; exit 1; }
if grep -qE '\{[A-Z_]+\}' PROMPT.md; then echo "Unfilled placeholders:"; grep -oE '\{[A-Z_]+\}' PROMPT.md | sort -u; exit 1; fi
if ! claude -p "reply with ok" >/dev/null 2>&1; then echo "Claude Code is not logged in. Run: claude, then /login"; exit 1; fi
tmux has-session -t proofread 2>/dev/null && { echo "Already running. Attach: tmux attach -t proofread"; exit 0; }
tmux new-session -d -s proofread "bash ~/work/proofread/run-loop.sh"
tmux set-option -t proofread remain-on-exit on
echo "Started. Attach with: tmux attach -t proofread   (detach: Ctrl-b d, do NOT press Ctrl-c)"
