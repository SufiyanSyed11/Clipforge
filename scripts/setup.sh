#!/usr/bin/env bash
# One-time setup (macOS/Linux). Needs Python 3.11+, Node 20+, ffmpeg on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e "backend[dev,pipeline]"
(cd web && npm install)
[ -f .env ] || cp .env.example .env
.venv/bin/clipforge-check
