#!/usr/bin/env bash
# Prompts for secrets with hidden input. Nothing is echoed or saved to shell history.
set -euo pipefail
cd ~/work/proofread
umask 077
tmp=.env.tmp; : > "$tmp"
ask() { local v; read -rsp "$1: " v; echo; [ -n "$v" ] || { echo "$1 is required"; rm -f "$tmp"; exit 1; }; printf '%s=%s\n' "$1" "$v" >> "$tmp"; }
opt() { local v; read -rsp "$1 (optional, Enter to skip): " v; echo; [ -z "$v" ] || printf '%s=%s\n' "$1" "$v" >> "$tmp"; }
ask PROPOSER_API_KEY
ask AGENT_API_KEY
ask BASELINE_API_KEY
opt OPENAI_API_KEY
opt GOOGLE_API_KEY
cat >> "$tmp" <<'CFG'
PROPOSER_MODEL=claude-opus-5-5
AGENT_MODEL=claude-sonnet-5
BASELINE_MODELS=claude-haiku-4-5-20251001,claude-sonnet-5,claude-opus-5-5
CFG
mv "$tmp" .env && chmod 600 .env
read -rsp "GitHub fine-grained token: " gh; echo
git config --global credential.helper store
printf 'https://x-access-token:%s@github.com\n' "$gh" > ~/.git-credentials && chmod 600 ~/.git-credentials
git config --global user.name "proofread-bot"
git config --global user.email "proofread-bot@users.noreply.github.com"
grep -qx '.env' .gitignore 2>/dev/null || echo '.env' >> .gitignore
echo "Saved .env (mode 600) and GitHub credentials."
