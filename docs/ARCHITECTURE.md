# ClipForge architecture and phased build plan

Status: **approved by the owner with the decisions in section 0.** Phase 0 and Phase 1 are
being built. Later phases still follow the per-phase gate: each phase ends with a QA report and
the owner's review before the next one starts.

## 0. Owner decisions (binding)

| # | Topic | Decision |
|---|---|---|
| 1 | Where it runs | Built and tested locally on the owner's **Windows PC** first. Docker is optional and not required for anything. The design keeps every infrastructure piece swappable so cloud deployment can be added in Phase 6. GPU-dependent components are chosen only after `clipforge-check` reports the real hardware. |
| 2 | Host identification | The app automatically suggests which speaker is the host; the owner confirms or changes it with one click per project. |
| 3 | Profanity | Prefer a different cut point. Bleep only when there is no suitable alternative. Never damage speech, and never cut at a whisper timestamp instead of real silence. |
| 4 | 60 fps | 30 fps sources are converted by **frame repetition** by default. Generating in-between frames is off and only runs if the owner explicitly enables it. |
| 5 | Branding | Default brand tag `#DoubleCoverage`. The logo is uploaded and changed in Settings. |
| 6 | Test podcasts | The owner supplies three episodes through the golden-sample workflow. **No test result is invented, and the quality bar is never reported as passed until those episodes have actually been run.** |
| 7 | B-roll restrictions | The spec's restrictions stay the default but are editable in Settings. They apply to every B-roll selection and are re-verified on the selected footage before export. |

## 1. Runtime design: Windows-first, no Docker required

Postgres, Redis and MinIO add setup and services on Windows that a single-user local app does
not need. Each one is replaced by a local default behind an interface, so the cloud version
swaps the implementation without touching pipeline code:

| Concern | Local default (Phase 0-5) | Cloud swap (Phase 6) | Interface |
|---|---|---|---|
| Database | SQLite file (`data/clipforge.db`) through SQLAlchemy | Postgres (change `DATABASE_URL`) | SQLAlchemy models |
| Job queue | Jobs table in the database; a separate worker process claims jobs atomically | Redis/Celery or a managed queue | `clipforge.jobs.queue` |
| Media storage | Local folder (`data/`) | S3-compatible storage | `clipforge.storage.Storage` |
| Live logs | Server-Sent Events from the job-events table | Same | `/api/jobs/{id}/events` |

RQ is not used: it relies on `fork()` and does not run on Windows. A database-backed queue
runs everywhere, survives restarts (a job left `running` by a crashed worker is re-queued), and
needs no extra service.

```
 Browser (Next.js, :3000)        API (FastAPI, :8000)            Worker process(es)
 ────────────────────────        ────────────────────            ──────────────────
 New project + rights box ─POST─▶ validate, store         ─jobs─▶ claim job → run step
 Live job log            ◀─SSE── job_events table        ◀─log── write events + QA report
 Host confirm / clips    ─PATCH▶ project settings              ingest → transcribe →
                                                               diarize → pick
                        SQLite (projects, jobs, events, clips, QA reports, settings)
                        data/ (media, transcripts, cached intermediates)
```

### Hardware-dependent choices

`clipforge-check` (Phase 0) inspects the PC and writes `data/env_report.json`: OS, CPU cores,
RAM, free disk, NVIDIA GPU and VRAM (via `nvidia-smi` and CTranslate2's CUDA device count),
ffmpeg/ffprobe/yt-dlp/Node versions, and whether the optional diarization stack is installed.
Defaults derived from it (all overridable in `.env`):

| Hardware found | Whisper model / device | Diarization backend |
|---|---|---|
| NVIDIA GPU with ≥ 6 GB VRAM and CUDA libs | `large-v3`, `cuda`, `float16` | `pyannote` if installed + Hugging Face token, else `basic` |
| GPU with < 6 GB VRAM | `medium`, `cuda`, `int8_float16` | same |
| CPU only | `medium` (≥ 16 GB RAM) or `small`, `cpu`, `int8`, 10-min chunks with overlap | `basic` |

`basic` diarization (built in, no PyTorch) clusters voice features over speech segments. It is
fine for two clearly different voices and weak on crosstalk; that's why the host confirmation
step exists. `pyannote` is the higher-quality optional backend.

## 2. Core design rules

- **A clip is data, not a render.** Each clip has an edit spec (JSON): source windows, cut
  points, B-roll slots, caption words, host insert, mix settings. Rendering (Phase 3+) is a
  pure function of the spec. UI edits change the spec, then re-render.
- **Every pipeline step is a pure function** `step(inputs, config) -> (outputs, QAReport)` in
  `clipforge.core`, with no database or queue code. Worker tasks are thin wrappers that load
  inputs, call the step, store outputs, and save the QA report.
- **Every step returns a `QAReport`** (machine-readable checks: name, passed, detail, timestamp
  when relevant). The UI shows it as a checklist on each job and clip.
- **Content-addressed cache** for intermediates, keyed by step, step version, inputs and
  config, so re-runs skip finished work and renders can resume.
- **Settings** (style preset, brand tag, logo, banned-content list, profanity mode, 60 fps mode)
  are stored in the database, edited in the UI, and passed into steps as config.

## 3. Folder layout

```
Clipforge/
├── CLAUDE.md                     # persistent rules
├── docs/                         # SPEC.md, ARCHITECTURE.md, reports/phase-N/
├── .env.example                  # every config key, no secrets
├── backend/
│   ├── pyproject.toml            # one Python package: clipforge
│   ├── clipforge/
│   │   ├── settings.py           # .env loading
│   │   ├── qa.py                 # QAReport shared by every step
│   │   ├── models.py             # pydantic: Transcript, Word, Speaker, ClipPlan, ...
│   │   ├── db.py                 # SQLAlchemy models + session
│   │   ├── storage.py            # Storage interface + local implementation
│   │   ├── jobs/                 # DB-backed queue, task registry, worker loop
│   │   ├── core/                 # pure steps
│   │   │   ├── ingest/           # rights gate, yt-dlp, upload, ffprobe
│   │   │   ├── transcribe/       # faster-whisper, chunking
│   │   │   ├── diarize/          # basic + pyannote backends, host suggestion
│   │   │   ├── pick/             # clip picker: prompt, Claude call, schema, rules
│   │   │   └── ...               # audio, cut, verify, frame, broll, captions, mix,
│   │   │                         # render, copy, learn (Phases 2-6)
│   │   ├── prompts/              # clip_picker.v1.md, ...
│   │   ├── api/                  # FastAPI app
│   │   └── tools/                # check_env, picker_harness, golden
│   └── tests/
├── web/                          # Next.js + TypeScript + Tailwind
├── scripts/                      # dev.ps1 (Windows), dev.sh (macOS/Linux)
├── golden/                       # owner's 3 episodes: episodes.json (media gitignored)
└── data/                         # runtime data (gitignored)
```

## 4. Key technical decisions

| Area | Decision | Why |
|---|---|---|
| Python | 3.11+ in a venv | faster-whisper / CTranslate2 wheels on Windows. |
| Transcription | faster-whisper with word timestamps + VAD; model/device from `clipforge-check` | As in the spec, adapted to the owner's hardware. |
| Clip picker | Claude API (`anthropic` SDK), model `claude-opus-5-5`, adaptive thinking, structured JSON output via a JSON schema, then pydantic validation and the hard rules enforced in code (30 s max, 18-30 s target, never pad) | "Strict JSON" made enforceable; the model can't push an over-long clip through. |
| Refusal fallback | Server-side fallback (`fallbacks: "default"`) on by default, `PICKER_FALLBACKS=off` disables it | Recommended for this model; a declined request is re-run instead of failing the job. |
| Cut engine (Phase 2) | numpy RMS on 48 kHz mono PCM, 10 ms hop; ffmpeg for cuts | Deterministic and testable. |
| Faces (Phase 3) | MediaPipe + PySceneDetect | Runs on CPU on Windows. |
| Captions (Phase 3) | ASS subtitles burned in by ffmpeg/libass | Remotion only if this falls short. |
| 60 fps | `fps=60` filter (frame repetition) by default; `minterpolate` only when `FRAME_INTERPOLATION=on` | Owner decision 4. |
| Frontend | Next.js App Router + TypeScript + Tailwind, dark UI | As in the spec. |

## 5. Phase plan and acceptance criteria

Every phase ends with `docs/reports/phase-N/README.md`: what was run, which inputs, outputs,
the QA report, **what is weak**, and **what was not verified**. A phase is reported complete
only when every acceptance criterion below passes. Criteria marked **[owner PC]** need the
owner's hardware, network or episodes and can't be run in the build sandbox; until they're
run, the phase is reported as "built, automated criteria pass, owner-PC criteria pending".

### Phase 0: Skeleton and end-to-end proof

Scope: project skeleton, settings, SQLite + DB queue, storage, QAReport, the API, the worker,
the web app shell, `clipforge-check`, dev scripts, CI. One trivial `ping` job goes end to end.
No pipeline features.

| ID | Acceptance criterion | How it's checked |
|---|---|---|
| P0-1 | Backend lint (ruff) and unit tests (pytest) pass | `pytest`, `ruff check` |
| P0-2 | Web type check and production build pass | `npm run typecheck`, `npm run build` |
| P0-3 | Queue: a job is claimed by exactly one worker; a job left `running` by a dead worker is re-queued; a failing task ends `failed` with its error logged | pytest |
| P0-4 | End to end: start API + worker + web, create a `ping` job from the UI, see live log lines arrive, see status `done` and the job's QA checklist | Playwright script `web/e2e/phase0.spec.ts` against the running stack |
| P0-5 | `clipforge-check` runs and writes `data/env_report.json` with hardware and tool findings and the derived defaults | pytest + manual run |
| P0-6 **[owner PC]** | On Windows: `scripts\dev.ps1` starts the stack and P0-4 works; `clipforge-check` reports the real GPU/CPU/RAM | Owner runs it |

### Phase 1: Ingest, transcript, clip picking

Scope (as in the spec): ingest (yt-dlp on permitted links, file upload), faster-whisper
transcription with word timestamps and speakers, the clip picker with strict JSON, a simple UI
listing ranked clips with scores and reasons, the picker test harness, and the golden-sample
workflow. No rendering.

| ID | Acceptance criterion | How it's checked |
|---|---|---|
| P1-1 | Rights gate: a project can't be created, and nothing is downloaded, unless the rights box is confirmed | pytest (API returns 422) + Playwright |
| P1-2 | Ingest: an uploaded file is stored and probed (duration, fps, streams); a URL is downloaded by **one** yt-dlp call into **one muxed file** (never separate audio/video joined afterwards); ffprobe confirms audio and video streams | pytest with a generated sample video; yt-dlp argument test |
| P1-3 | Transcription: produces words with start/end times; CPU chunking with overlap merges without duplicated or dropped words; device/model come from the env report | pytest (chunk merge, config) |
| P1-4 | Diarization: every word gets a speaker; the `basic` backend separates two clearly different synthetic voices; the host is auto-suggested with a reason | pytest |
| P1-5 | Host confirmation: one click in the UI sets or changes the host for the project, and the transcript/picker use it | pytest + Playwright |
| P1-6 | Picker output is validated against the schema; invalid output is retried, then fails loudly; **no clip over 30 s is ever accepted**; clips outside 18-30 s are flagged; fewer clips are returned rather than padded; risk flags (profanity with times, unverifiable claims, private info, defamation) are carried through | pytest with recorded/fake model responses |
| P1-7 | Picker harness: runs any prompt version on any saved transcript and writes a side-by-side comparison | pytest + CLI run |
| P1-8 | UI: new project (link or file + rights box) → live log → host confirm → ranked clip list with score, reasons, hook/payoff, titles, risk flags, QA checklist | Playwright (pipeline plumbing run, models stubbed and labelled as such) |
| P1-9 **[owner PC]** | Real transcription with faster-whisper on the owner's hardware | Owner runs the golden workflow |
| P1-10 **[owner PC]** | Real clip picking with the Claude API key | Owner runs the golden workflow |
| P1-11 **[owner PC]** | Golden run on the owner's 3 episodes: transcripts, picked clips and QA reports written to `docs/reports/phase-1/`, then reviewed for what is weak | `clipforge-golden run` |

### Phase 2: Cut-point engine and verification gate (highest priority)
Unchanged from the spec. Exit: clean A/V cuts for every picked clip and a join-by-join QA report
proving zero clipped words on all 3 golden episodes, including the noisy one.

### Phase 3: Framing, captions, audio mix
Unchanged from the spec, plus decision 4 (frame repetition by default). Exit: 3 finished clips
plus a list of what looks off.

### Phase 4: B-roll engine
Unchanged from the spec, plus decision 7: the banned-content list comes from Settings, every
selection is checked against it, and the export gate re-verifies the chosen footage.

### Phase 5: Copy pack, editor UI, export
Unchanged from the spec, plus decision 5 (`#DoubleCoverage` default, logo upload in Settings).

### Phase 6: Learning loop and polish
Unchanged from the spec, plus the cloud deployment path (Postgres, object storage, managed
queue) behind the interfaces from section 1.

## 6. Risks and honest limits

- **Diarization on crosstalk** is the weakest link for host/guest logic. The host confirmation
  step and (in Phase 3) per-speaker mouth movement mitigate it.
- **Stock B-roll rarely matches specific real events.** WEAK flags + the swap screen are the
  real answer.
- **CPU-only transcription** works but is slow on long episodes.
- **The build sandbox can't reach Hugging Face or YouTube and has no Claude API key**, so real
  transcription, real downloads and real clip picking are verified only on the owner's PC.
