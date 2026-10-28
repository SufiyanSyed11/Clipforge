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

Open http://localhost:3000.

### Local AI model (free, no API key)

Clips are picked by a local model through [Ollama](https://ollama.com). Nothing is sent to a
paid service and no API key is needed.

1. Install Ollama for Windows from https://ollama.com/download (it runs in the tray and
   listens on `http://127.0.0.1:11434`).
2. Download the one model ClipForge needs (~2.5 GB):

   ```powershell
   ollama pull qwen3:4b
   ```

3. Check it: `.\.venv\Scripts\clipforge-check.exe` should print `Clip picker: OK`, and the
   System page shows "AI models: ready".

`setup.ps1` offers to run step 2 for you if Ollama is installed. Jobs check that Ollama is
running and the model is downloaded before they start, and stop with a clear message if not.

On a 6 GB GPU (RTX 3050) the defaults are: Whisper `large-v3` in int8 and a 12k-token LLM
context; Ollama is asked to unload its model before transcription so the two never compete
for VRAM. Long episodes are picked in overlapping parts that fit the context.

To use a different local model: `ollama pull <model>` and set `OLLAMA_MODEL=<model>` in
`.env`. To use Claude instead (paid): set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

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
