# Running ClipForge: hardware, performance, storage, security

ClipForge is a **single-user app for your own Windows PC**. The setup steps are in the
[README](../README.md); this page covers what runs where, what to expect from an RTX 3050
6 GB, the CPU fallback, disk use, the security model and the path to a cloud version.

Numbers marked *measured* were measured in the build sandbox (4-core Intel Xeon 2.1 GHz CPU,
no GPU). Everything else is an estimate from model sizes and has **not** been measured on
your PC yet; the golden run records the real timings.

## 1. What runs where

| Step | RTX 3050 6 GB + 16 GB RAM (your PC) | No usable NVIDIA GPU (CPU fallback) |
|---|---|---|
| Transcription (faster-whisper) | `large-v3`, CUDA, int8_float16 (about 2-3 GB VRAM) | `medium` int8 on CPU (`small` below 15 GB RAM), 10-minute chunks |
| Speaker separation | `basic` (MFCC, CPU); `pyannote` optional | same |
| Clip picking, B-roll queries, copy | Ollama `qwen3:4b`, 12k-token context (about 4.3 GB VRAM) | same model on CPU, 8k context |
| B-roll vision check (optional) | Ollama `qwen2.5vl:3b` (extra ~3 GB download) | same on CPU (slow) |
| Face detection / recognition | OpenCV YuNet + SFace on CPU | same |
| Render | NVENC (`h264_nvenc`) when the driver can encode, else libx264 | libx264 |
| Mix, loudness | ffmpeg on CPU | same |

`clipforge-check` decides these from the real hardware and writes `data/env_report.json`;
the System page shows the result. Override any choice in `.env` (`WHISPER_MODEL`,
`WHISPER_DEVICE`, `OLLAMA_NUM_CTX`, `RENDER_ENCODER`).

### 6 GB of VRAM: one big model at a time

Whisper and the language model don't fit in 6 GB together, so ClipForge hands the GPU over
explicitly:

1. Episode transcription: Ollama is asked to unload its model first.
2. Clip picking: Whisper is done; Ollama loads `qwen3:4b`.
3. Each clip build: B-roll queries (and the vision model, if enabled) run on Ollama; then
   Ollama's text **and** vision models are unloaded before the verification gate
   re-transcribes the cut on the GPU. (Before Phase 6 only step 1 unloaded; a build could
   run out of VRAM. Fixed and tested.)

If you still see CUDA "out of memory" errors (another app holding VRAM, a browser with
hardware acceleration, a game), close it or set `WHISPER_COMPUTE_TYPE=int8`.

### Speed expectations

- *Measured (CPU, 4 cores, libx264):* building one ~22 s clip at 1080x1920 60 fps — cut,
  gate, captions, mix, render and render check — took 27-30 s, of which render + check
  24-25 s. This used the synthetic test video (no B-roll downloads, test-double
  transcription), so real clips will take longer.
- *Estimate:* NVENC on the RTX 3050 should cut render time substantially; Python-side frame
  composition (camera path, captions, B-roll) stays on the CPU and is likely the limit.
- *Estimate:* `large-v3` on the RTX 3050 transcribes roughly 5-10x faster than real time;
  on CPU, `medium` int8 is closer to real time or slower. A 2-hour episode on CPU can take
  hours. The golden run prints the real number.
- *Estimate:* `qwen3:4b` on the GPU answers a picker request in tens of seconds; on CPU,
  several minutes per transcript part. Long episodes are split into overlapping parts that
  fit the context.

### CPU fallback behaviour

Everything works without a GPU; it's slower. Nothing silently switches to a weaker model
mid-job: the model and device are fixed by `clipforge-check` (or `.env`), logged at the start
of each job, and shown on the System page. If CUDA is found but CTranslate2 can't use it
(missing cuBLAS/cuDNN), the check says so and Whisper uses the CPU until that's fixed (see
the faster-whisper README for the CUDA 12 libraries).

## 2. Disk use

| What | Size (estimate) | Re-creatable? |
|---|---|---|
| Source episode (≤1080p) | ~1-3 GB per hour | no (re-download needs the URL again) |
| `audio16k.wav` (transcription) | ~115 MB per hour | yes |
| `audio48k.wav` (cutting) | ~350 MB per hour | yes |
| Per clip: `voice.wav`, `mix.wav`, `mix/` stems | ~10-20 MB | yes |
| Per clip: `render.mp4` | ~30-80 MB | rebuild |
| Export pack (folder + zip) | ~2x the render | re-export |
| `data/broll_cache` | grows with every B-roll search | yes (re-downloads) |
| Ollama models | `qwen3:4b` ~2.5 GB (+ `qwen2.5vl:3b` ~3 GB if enabled) | — |
| Whisper `large-v3` | ~3 GB (Hugging Face cache) | — |

- Downloads, audio extraction and renders **refuse to start** when free space would drop
  below `MIN_FREE_GB` (default 5 GB), with a message saying how much is free.
- **System → Storage** shows where the space goes and clears re-creatable files and the
  B-roll cache. Source episodes, renders and export packs are never touched by those buttons.
  Deleting a whole project asks for confirmation and keeps its export packs.
- Uploads are capped at `MAX_UPLOAD_GB` (default 20).
- Only one large model is downloaded by default (`qwen3:4b`). The vision model is opt-in.

## 3. Security model

ClipForge has **no user accounts**. It is safe on your own PC because:

- The API and web app listen on `127.0.0.1` only.
- The API rejects requests whose `Host` header isn't localhost (or a name you add in
  `ALLOWED_HOSTS`), which blocks DNS-rebinding attacks from websites you visit.
- Every write (POST/PUT/PATCH/DELETE) needs the `X-ClipForge` header, which a cross-site
  form can't send; CORS allows only the web app's origin.
- Files are served only from inside a project's folder; uploads can't choose their path;
  branding files must live in `data/branding`.
- API keys and OAuth tokens stay on the server: the UI only sees "set / not set". The
  YouTube token is stored in `data/secrets/` with owner-only permissions where the OS
  supports it (on Windows, it relies on your user profile's permissions).
- YouTube access is read-only unless you tick "allow scheduled private uploads"; uploads are
  always private with a scheduled publish time, never public immediately.
- Dependency audits at the end of Phase 6: `npm audit` 0 vulnerabilities (after moving from
  Next.js 14.2.15 to 16.4.3), `pip-audit` 0 known vulnerabilities.

**Don't expose it to the internet or your LAN as-is** (port forwarding, `0.0.0.0`, a
tunnel). Anyone who can reach the API can use it.

## 4. Path to a cloud version (not built)

Each infrastructure piece sits behind an interface, so a hosted version swaps
implementations rather than pipeline code:

| Piece | Today | Cloud swap | Status |
|---|---|---|---|
| Database | SQLite | Postgres via `DATABASE_URL` | SQLAlchemy models are portable; **not tested on Postgres** |
| Job queue | DB table + worker processes | same on Postgres, or a managed queue | works with several workers; not load-tested |
| Media storage | local `data/` folder | S3-compatible storage | interface exists (`clipforge.storage`); **no S3 implementation** |
| Auth | none (local only) | accounts, sessions, per-user data | **not built; required before any hosting** |
| GPU work | your PC | a GPU worker pool | not built |

A hosted version also needs: per-user OAuth tokens and keys (encrypted), quotas, HTTPS, and
a review of the rights-confirmation flow for multiple users.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| "Clip picker unavailable: Ollama is not running" | Start Ollama (tray icon) or run `ollama serve` |
| "model qwen3:4b is not downloaded" | `ollama pull qwen3:4b` |
| Whisper on CPU although you have an NVIDIA GPU | `clipforge-check` explains why; usually missing CUDA 12 cuBLAS/cuDNN 9 libraries |
| Render uses libx264 on an NVIDIA PC | Update the NVIDIA driver; `RENDER_ENCODER=h264_nvenc` forces NVENC (fails loudly if unusable) |
| "Not enough free disk space" | System → Storage → clear re-creatable files / B-roll cache, or free space elsewhere |
| B-roll slots all WEAK | Add `PEXELS_API_KEY` / `PIXABAY_API_KEY`, or upload your own footage per slot |
| UI buttons return 403 | Open the app at `http://localhost:3000` (not a LAN IP) and make sure the web app and API are the same version |
