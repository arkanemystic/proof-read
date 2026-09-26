#!/usr/bin/env bash
# Run biject-oss without Docker: lean-worker on 127.0.0.1:19000 (builds proofread/policies/lean with
# lake on start), backend on 127.0.0.1:18002. Needs elan on PATH and uv. Ctrl-C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${BIJECT_LOG_DIR:-$ROOT/logs/biject-oss}"
mkdir -p "$LOG_DIR"
if [ -f "$ROOT/biject-local/.env" ]; then set -a; . "$ROOT/biject-local/.env"; set +a; fi

POLICY_ENV_DIR="$ROOT/proofread/policies/lean" HOST=127.0.0.1 PORT=19000 \
  python3 "$ROOT/biject-oss/lean-worker/worker.py" > "$LOG_DIR/worker.log" 2>&1 &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT

for _ in $(seq 1 300); do
  if curl -s 127.0.0.1:19000/health | grep -q '"status": "ok"'; then break; fi
  if ! kill -0 $WORKER 2>/dev/null; then echo "lean-worker exited; see $LOG_DIR/worker.log"; exit 1; fi
  sleep 2
done
curl -s 127.0.0.1:19000/health | grep -q '"status": "ok"' || { echo "lean-worker not ready; see $LOG_DIR/worker.log"; exit 1; }
echo "lean-worker ready; backend on http://127.0.0.1:18002 (worker log: $LOG_DIR/worker.log)"

cd "$ROOT/biject-oss/backend"
LEAN_WORKER_URL=http://127.0.0.1:19000 AUDIT_LOG_PATH="$LOG_DIR/audit.log" \
  uv run --project "$ROOT" --with 'fastapi==0.115.6' --with 'uvicorn[standard]==0.34.0' \
  uvicorn app.main:app --host 127.0.0.1 --port 18002
