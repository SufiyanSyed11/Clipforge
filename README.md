# ClipForge

Turns a podcast (YouTube link you have rights to, or an uploaded file) into ranked,
ready-to-post vertical short clips.

- Spec: [docs/SPEC.md](docs/SPEC.md)
- Architecture, decisions and acceptance criteria: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Phase reports: [docs/reports/](docs/reports/)
- Rules for Claude Code: [CLAUDE.md](CLAUDE.md)

## Run it on Windows

Prerequisites: Git, Python 3.11-3.13 (3.12 recommended: `winget install Python.Python.3.12`),
Node 20+ (`winget install OpenJS.NodeJS.LTS`), ffmpeg on PATH (`winget install Gyan.FFmpeg`).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1   # once
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1     # every time
```

Open http://localhost:3000. Put your Claude API key in `.env` (`ANTHROPIC_API_KEY=`).

`clipforge-check` (run by both scripts) inspects the PC and picks the Whisper model and device:
a usable NVIDIA GPU gets `large-v3` on CUDA, otherwise it runs on the CPU.

## Run it on macOS/Linux

```bash
scripts/setup.sh
scripts/dev.sh
```

## Tests

```bash
.venv/bin/pytest -q backend
.venv/bin/ruff check backend
cd web && npm run typecheck && npm run build
cd web && npx playwright test   # needs the stack running
```
