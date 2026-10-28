#!/usr/bin/env bash
# One-time setup (macOS/Linux). Needs Python 3.11+, Node 20.9+, ffmpeg on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a<20||(a===20&&b<9)) { console.error("Node 20.9+ is required (Next.js 16)"); process.exit(1) }'
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e "backend[dev,pipeline]"
(cd web && npm install)
[ -f .env ] || cp .env.example .env
.venv/bin/clipforge-check
