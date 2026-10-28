#!/usr/bin/env bash
# Start API, worker and web app (macOS/Linux). Ctrl+C stops all three.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=".venv/bin"
[ -x "$VENV/python" ] || { echo "Run scripts/setup.sh first"; exit 1; }

"$VENV/clipforge-check" >/dev/null || true
"$VENV/clipforge-api" & API=$!
"$VENV/clipforge-worker" & WORKER=$!
(cd web && npm run dev) & WEB=$!
trap 'kill $API $WORKER $WEB 2>/dev/null' INT TERM EXIT
echo "ClipForge: http://localhost:3000  (API http://127.0.0.1:8000)"
wait
