# ClipForge: final build report

Date: 2026-10-28. Branch `claude/google-docs-link-3qo0tl`.

## Bottom line

All six phases are implemented end to end: a real pipeline, real UI and real exports, with
no placeholder screens. Every automated test, lint, type check, build and dependency audit
passes.

**ClipForge is not yet proven production-ready.** The quality bar in the spec (zero clipped
words, zero profanity leaks, clean B-roll, correct loudness on 3 real podcasts) has **not
been run**. The build environment can't reach YouTube, Hugging Face, Ollama's model library,
Pexels or Pixabay, and has no GPU. Every automated result below uses synthetic audio/video,
test doubles or mock HTTP transports, and is labelled that way. The next step is yours: the
golden run in section 6.

## 1. Completed features

| Phase | What works | Report |
|---|---|---|
| 0 | FastAPI + SQLite + DB-backed job queue (Windows-friendly, crash recovery, heartbeats), worker process, live job logs over SSE, QA checklists, `clipforge-check` hardware report, Next.js UI, setup/dev scripts for Windows and macOS/Linux, CI | `reports/phase-0` |
| 1 | Rights confirmation gate before any download; yt-dlp single muxed file or upload; ffprobe QA; faster-whisper word timestamps (GPU/CPU chosen from hardware); speaker separation (basic MFCC, optional pyannote); host auto-suggestion with one-click confirm; clip picker on the local model (Ollama + `qwen3:4b`) with JSON schema, validation, retries, long-episode chunking, 18-30 s with a hard 30 s max, profanity/risk flags; golden-episode tool | `reports/phase-1` |
| 2 | Cut-point engine: 10 ms RMS trace, cuts only at the minimum-RMS point of real silence, whisper-drift correction, extension to the next natural pause in noisy audio, frame-grid snapping, pause shortening only in true silence, profanity avoided by a different seam or bleeped (mute + 1 kHz tone, censored caption); verification gate (re-transcription diff, join RMS 40-60 ms, onset 0.3 s / decay 0.45 s, A/V duration and frame count), failing loudly with timestamps | `reports/phase-2` |
| 3 | Face detection/recognition (YuNet/SFace), active-speaker camera path with smoothing and push-in, TV-wall face rejection, host-reaction insert (listening shot, lips still, different per clip), one-word bold captions with emphasis, logo top right, colour grade, 30→60 fps by frame repetition (interpolation opt-in), mix at -14 LUFS with music 19 dB under (ducked) and -1 dB limiter, NVENC/libx264 render | `reports/phase-3` |
| 4 | B-roll engine: a beat every 2-3 s (~62 % cutaway), beat-specific queries from the local model, Pexels/Pixabay/permitted YouTube/your library (1080p+ only), contact sheets, vision check (optional local model) or owner approval, banned-content and reject rules from Settings re-verified before export, podcast footage never used (URL, file hash, frame hash), `WEAK: needs manual cover` instead of junk | `reports/phase-4` |
| 5 | Copy pack (title with one emoji, YouTube description with call to comment, Instagram caption with brand tag + max 5 tags, X post without emojis, no credit line unless set), rules enforced in code; clip editor (preview, waveform trims snapped to silence, caption fixes, host-insert toggle, B-roll review/upload, copy editing); export packs (video, transcript, shot list, captions, copy, QA, hashed manifest), only for verified clips | `reports/phase-5` |
| 6 | Learning loop (YouTube OAuth or Studio/vidIQ CSV, insights feeding the picker and copy writer, "channel data" notes on clips); posting time suggestion, `.ics`, optional **private scheduled** YouTube upload; storage management and low-disk refusal; security hardening; profanity-leak gate check; 6 GB VRAM handoff fix; full-pipeline golden tool; dependency upgrades | `reports/phase-6` |

Cross-cutting: job cancel and retry from the UI (retries reuse cached intermediates), clear
errors when Ollama or a model is missing (checked before long steps), no API key needed for
normal use, and test doubles are only active with `FAKE_MODELS=true` and are labelled
`TEST DOUBLE` / `UNVERIFIED-TEST` everywhere they appear.

## 2. Test results (final run, this environment)

| Check | Result |
|---|---|
| Backend tests (`pytest`, 20 files) | **197 passed**, 0 failed (6 min 37 s) |
| Browser tests (Playwright, Chromium, serial) | **5 passed**: ping job, upload → rights gate → host → clips, URL without rights refused, learning CSV + storage, full workflow settings → import → edit → copy → export → download |
| `ruff check` / `ruff format --check` | clean |
| `npm run lint` (ESLint 9) / `npm run typecheck` / `npm run build` (Next 16.4.3) | clean |
| `npm audit` (all dependencies) | 0 vulnerabilities |
| `pip-audit` (backend venv) | 0 known vulnerabilities |
| Cut-engine stress grid (synthetic speech with whisper-like timing errors) | 480/480 sentence-aligned clips exact; mid-sentence requests 232-234/240 keep every edge word, and the gate catches 100 % of the rest |
| Measured render (4-core CPU, libx264, synthetic source) | ~22 s clip at 1080x1920 60 fps: 27-30 s per build, 24-25 s of it render + check |

What these tests **don't** prove: transcription accuracy, diarization on real voices, clip
choice quality from `qwen3:4b`, framing on real multi-camera footage, real B-roll relevance
and cleanliness, how the mix sounds, and real-world clipped-word rates. Those are the golden
run.

## 3. Known limitations

1. **Quality bar not run** on real podcasts (see above). Under test doubles the gate always
   reports "re-transcription by a real model: failed", so nothing is ever marked verified.
2. **Basic diarization** is tuned on synthetic voices; crosstalk and similar voices may
   confuse it. Host confirmation catches the main consequence; pyannote is optional.
3. **A 4B local model** may need retries to follow every copy/picker rule; failures are
   explicit, never silent. Title "curiosity gap" quality is a judgement call for you.
4. **Face tracking and lip-movement checks** were only exercised on scripted detections.
   TV-wall rejection is size-based.
5. **B-roll**: stock footage rarely matches specific real events, so expect WEAK slots for
   named people and moments. With the vision check off (the default), you are the text/logo
   check via contact sheets.
6. About **3 % of mid-sentence boundaries** in synthetic stress tests start a word late
   before the gate flags them as "needs review"; real-world rate unknown.
7. **YouTube OAuth/upload** has only been tested against mock transports.
8. **No user accounts**: local use only. Don't expose it to your network or the internet.
   Cloud hosting needs auth, an S3 storage implementation and Postgres testing (not built).
9. **Python type checking isn't enforced** (mypy reports 83 non-runtime findings);
   TypeScript is strict and checked.
10. The Windows scripts (`setup.ps1`, `dev.ps1`) were reviewed but not executed here (no
    Windows/PowerShell in the build environment).

## 4. External credentials

| Credential | Required? | Used for | Where |
|---|---|---|---|
| None | — | Normal operation: Ollama + `qwen3:4b` is free and local | — |
| `PEXELS_API_KEY`, `PIXABAY_API_KEY` | Optional (free) | Stock B-roll. Without them, only your own uploads and permitted YouTube sources | pexels.com/api, pixabay.com/api/docs |
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | Optional (free) | Learning loop from your channel; scheduled private uploads. Without them, use CSV import | Google Cloud → enable YouTube Data API v3 + YouTube Analytics API → OAuth client "Desktop app" |
| `HF_TOKEN` | Optional | Only for the pyannote diarization backend | huggingface.co |
| `ANTHROPIC_API_KEY` | Optional (paid) | Only if you set `LLM_PROVIDER=anthropic` or `VISION_PROVIDER=anthropic` | console.anthropic.com |

All of these go in `.env` (never committed). The UI shows only "set / not set". ClipForge
never asks for cookies or passwords.

## 5. Setup on your Windows PC

In a new folder, **not inside `C:\VideoEditor`**:

```powershell
git clone -b claude/google-docs-link-3qo0tl https://github.com/SufiyanSyed11/Clipforge.git C:\ClipForge
```

```powershell
cd C:\ClipForge
```

1. Prerequisites: Python 3.12 (`winget install Python.Python.3.12`), Node 20.9+
   (`winget install OpenJS.NodeJS.LTS`), ffmpeg (`winget install Gyan.FFmpeg`), Ollama
   (https://ollama.com/download).
2. One-time setup. This creates `.venv`, installs everything, copies `.env.example` to
   `.env`, runs `clipforge-check` and offers to pull `qwen3:4b`:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
   ```

3. Check the hardware report: Whisper should be `large-v3` on `cuda` for the RTX 3050. If it
   says CTranslate2 can't use CUDA, install the CUDA 12 cuBLAS and cuDNN 9 libraries (see
   the faster-whisper README) and run `.\.venv\Scripts\clipforge-check.exe` again.
4. Optional keys in `.env` (section 4).
5. Start everything (API, worker, web), then open http://localhost:3000:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
   ```

6. System page: "AI models: ready", the ping job passes, and Storage shows free space.
7. Settings: upload your logo (and a royalty-free music bed); check the B-roll banned-content
   list.

More detail: [DEPLOY.md](DEPLOY.md) (GPU/CPU behaviour, disk use, security, troubleshooting).

## 6. Remaining real-podcast validation (your steps)

1. Pick 3 episodes you have rights to: one clean studio episode, one noisy/crosstalk, and one
   multi-camera show with a TV wall.
2. Run `.\.venv\Scripts\clipforge-golden.exe init` and fill in `golden\episodes.json`
   (`source`, `rights_confirmed: true`).
3. Stop `dev.ps1`'s worker (or the whole dev stack), then run
   `.\.venv\Scripts\clipforge-golden.exe run`. It transcribes, picks and builds every clip,
   and writes `docs\reports\golden\golden-summary.md` with timings and a quality-bar table per
   clip.
4. Start `dev.ps1` again and open the `golden:ep1..3` projects. For each clip:
   - watch it with sound on and listen at every cut (clipped words, breaths, music);
   - check captions match the words, the framing follows the speaker, and the host insert
     (if any) shows the host listening;
   - review the B-roll contact sheets: no text, logos, watermarks, score bugs or wrong
     people. Approve or replace, and leave WEAK where nothing fits;
   - read the copy pack; export the clips that pass.
5. Confirm on the summary: every row PASS for every clip (zero clipped words, zero profanity
   leaks, voice -14 LUFS, music under the voice, limiter, A/V duration, B-roll checked).
   Write "what is weak" per episode.
6. Send me the summary (or commit it). I'll fix what's weak and re-run, and only after a
   clean pass on all 3 will the quality bar be reported as met.
7. Optional: connect YouTube on the Learning page (or import a Studio CSV), and schedule one
   clip as a private upload to confirm the upload path.

## 7. Top 10 next improvements

1. Fix whatever the golden run shows (that's the priority by definition).
2. Bundle a royalty-free default music bed so the "BGM under the voice" check is always
   exercised.
3. Enforce mypy in CI after cleaning up its 83 findings.
4. Lip-contour landmarks instead of mouth-region pixel difference for speaker/host checks.
5. Key B-roll decisions by beat text, not slot number, so trims don't shift approvals.
6. A resumable, chunked YouTube upload (current upload sends the whole file at once; fine
   for ≤30 s clips).
7. GPU-accelerated frame composition (or ffmpeg filters) to cut render time further.
8. Per-episode colour correction before the shared grade.
9. A Windows installer / tray launcher instead of PowerShell scripts.
10. Accounts, S3 storage and Postgres tests if you ever want a hosted version.
