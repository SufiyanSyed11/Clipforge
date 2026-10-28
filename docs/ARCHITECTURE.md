# ClipForge architecture and phased build plan

Status: **proposal, awaiting the owner's "go"**. No code is written until it's approved.

## 1. System overview

```
 Browser (Next.js)                API (FastAPI)                 Workers (RQ on Redis)
 ─────────────────                ─────────────                 ─────────────────────
 New project form  ──POST /jobs──▶ validate, rights check ──enqueue──▶ ingest
 Live job log      ◀──SSE /events── job state in Postgres ◀──progress── transcribe
 Clip cards/editor ──PATCH clip──▶ edit spec (JSON)       ──enqueue──▶ pick → cut → verify
 Export            ──POST export─▶ build pack                         → frame → broll
                                                                       → captions → mix
                                                                       → render → copy
                          Postgres (projects, jobs, clips, QA reports, edit specs)
                          Object storage (S3 / MinIO locally): media + cached intermediates
```

- **The clip is described by data, not by a render.** Each clip has an *edit spec* (JSON): source
  windows, cut points, B-roll slots, caption words, host insert, mix settings. Rendering is a
  pure function `render(edit_spec, assets) -> mp4`. Editing in the UI changes the spec, then
  re-renders. This makes re-renders and resuming cheap.
- **Every pipeline step is a pure function** `step(inputs, config) -> (outputs, QAReport)` in
  `clipforge_core`, with no database or queue code. Workers are thin wrappers that load inputs,
  call the step, store outputs, and save the QA report. This keeps every step unit-testable
  against golden samples.
- **Content-addressed cache.** Intermediates are keyed by `hash(step, step_version, inputs,
  config)`, so a crashed or re-run job skips finished work (resumable renders).

## 2. Folder layout

```
clipforge/
├── CLAUDE.md                  # persistent rules
├── docs/                      # SPEC.md, ARCHITECTURE.md, phase reports
├── .env.example               # every config key, no secrets
├── docker-compose.yml         # postgres, redis, minio, api, worker, web
├── core/                      # Python package: clipforge_core (pure, no I/O frameworks)
│   ├── clipforge_core/
│   │   ├── ingest/            # yt-dlp wrapper, ffprobe, rights gate
│   │   ├── transcribe/        # faster-whisper, chunking, diarization (pyannote)
│   │   ├── pick/              # clip-picker prompt, Claude call, JSON schema, ranking
│   │   ├── audio/             # RMS trace, silence finder, loudness, bleep
│   │   ├── cut/               # cut-point engine, frame snapping, pause removal
│   │   ├── verify/            # verification gate
│   │   ├── frame/             # face detection, speaker tracking, camera path, host insert
│   │   ├── broll/             # beats, stock search, contact sheets, vision rejection
│   │   ├── captions/          # word correction, one-word captions (ASS), safe zones
│   │   ├── mix/               # LUFS normalize, BGM ducking, limiter, SFX
│   │   ├── render/            # edit spec -> ffmpeg filtergraph
│   │   ├── copy/              # title / description / IG / X writers
│   │   ├── learn/             # YouTube analytics + vidIQ features
│   │   ├── qa.py              # QAReport model shared by every step
│   │   └── models.py          # pydantic: Transcript, Word, ClipPlan, EditSpec, ...
│   ├── prompts/               # clip_picker.v1.md, copy_writer.v1.md, vision_qa.v1.md
│   └── tests/
│       ├── unit/              # synthetic audio/video fixtures
│       └── golden/            # 3 real podcasts (local only, gitignored media)
├── api/                       # FastAPI app: routes, DB models (SQLAlchemy), SSE
├── worker/                    # RQ job wrappers around clipforge_core steps
├── web/                       # Next.js + TypeScript + Tailwind
├── tools/
│   ├── picker_harness/        # run any prompt version on any transcript, diff the results
│   └── qa_report/             # print a join-by-join QA report for a clip
└── output/                    # local runs, one folder per project (gitignored)
```

## 3. Key technical decisions (proposed)

| Area | Proposal | Why |
|---|---|---|
| Queue | **RQ + Redis** | Simpler than Celery; long ffmpeg/whisper jobs fit RQ's model. |
| DB / storage | Postgres + MinIO locally (S3 in prod) | As in the spec; MinIO keeps local dev identical to prod. |
| Transcription | faster-whisper `large-v3` (GPU) / `medium` int8 (CPU, 10-min chunks, overlap) | Jobs never time out on CPU. |
| Diarization | pyannote.audio 3.x, then map speakers to host/guest by talk share + user confirmation | The host-reaction rule needs to know who the host is. |
| Clip picker | Claude API, strict JSON through tool use with a pydantic schema; retries when the JSON doesn't validate | "Strict JSON" made enforceable. |
| Cut engine | numpy RMS on 48 kHz mono PCM, 10 ms hop; ffmpeg for the actual cuts | Deterministic and testable. |
| Verification | Re-transcribe with faster-whisper; normalized word diff | As in the spec. |
| Faces | MediaPipe face detection + face mesh (mouth openness); shot-change detection with PySceneDetect | Active speaker = mouth movement correlated with the diarized speaker. |
| Camera path | Per-shot crop track, smoothed (Savitzky-Golay/one-euro filter), push-in 100%→~108% across the clip | Slow, pro-looking camera moves. |
| B-roll | Pexels + Pixabay APIs; permitted YouTube via yt-dlp; contact sheets → Claude vision | As in the spec. |
| Captions | ASS subtitles burned in by ffmpeg (libass), one word per event, emphasis styling | Fast; Remotion only if this falls short. |
| Mix | ffmpeg `loudnorm` (two-pass) → `sidechaincompress` → `alimiter` | All in one filtergraph. |
| Output | H.264 High, 1080x1920, 60 fps, AAC 48 kHz | Works on every platform. |
| Frontend | Next.js App Router, TanStack Query, wavesurfer.js for waveform + trim | As in the spec. |

## 4. Phased plan

Every phase ends with a **phase report** in `docs/reports/phase-N.md`: what was run (which
podcasts), output samples, the QA report, **what is weak**, and **what was not verified**.

### Phase 0: Scaffold (small)
Repo skeleton, docker-compose, `.env.example`, pydantic models, `QAReport`, CI (ruff, mypy,
pytest, eslint, tsc). One trivial job going end to end through API → worker → UI log.

### Phase 1: Ingest, transcript, clip picking
- Rights-confirmation checkbox (required) → yt-dlp single muxed download (`-f "bv*+ba/b"
  --merge-output-format mp4`, one invocation) or file upload → ffprobe metadata.
- faster-whisper with word timestamps + VAD, chunked on CPU; pyannote diarization; host/guest
  labelling (auto-guess + confirm in the UI).
- Clip picker v1 with schema validation, risk flags, and enforcement of the hard 30 s max after
  the model replies.
- `tools/picker_harness`: run prompt vN on a saved transcript and show the results side by side.
- UI: new project → live log → ranked clip list with scores, reasons, transcript snippet.
- **Exit:** 3 podcasts transcribed and picked; transcripts + clips shown; weak points listed.

### Phase 2: Cut-point engine and verification gate (highest priority)
- RMS trace (10 ms), noise-floor estimate, silence finder that adapts to noise (dips relative to
  the local floor), boundary search window ±600 ms, extend-to-next-pause fallback.
- Frame-grid snapping, 5-10 ms fades at joins, pause removal with threshold + margin.
- Profanity: seam re-selection first, else bleep (mute + 1 kHz) + censored caption (setting).
- Verification gate: re-transcription diff, RMS 40-60 ms around each join, onset 0.3 s / decay
  0.45 s, A/V duration + frame-count equality. Fails with exact timestamps.
- Synthetic unit fixtures (tone + noise + speech-like bursts) plus golden tests.
- **Exit:** clean A/V cuts for every picked clip; a join-by-join QA report proving zero clipped
  words on all 3 podcasts, including the noisy one.

### Phase 3: Framing, captions, audio mix
- Shot detection → per-shot face tracks → active speaker → smoothed crop path + push-in.
  Reject faces outside the speaker region (TV walls).
- Host-reaction insert (frame-difference + mouth-closed check), a different shot per clip.
- One-word ASS captions from corrected words, emphasis, logo top right, safe zones.
- Mix: -14 LUFS voice, BGM -19 dB with ducking, limiter -1 dB. Render 60 fps.
- **Exit:** 3 finished clips plus a list of what looks off.

### Phase 4: B-roll engine
- Beat extraction (2-3 s), query generation per beat, Pexels/Pixabay/permitted-YouTube search,
  1080p+ filter, window selection without scene cuts.
- Contact sheets → Claude vision rejection (text, logos, watermarks, bugs, lower-thirds, bars,
  wrong person/event, low-res, banned-content list). Identity check for named people.
- WEAK slots; never podcast footage (enforced by source-hash check).
- UI contact-sheet review + swap.

### Phase 5: Copy pack, editor UI, export
- Copy writers (title, YT description, IG caption max 5 tags, X post).
- Per-clip editor: snap-to-silence trim (uses the Phase 2 engine), caption word edits, B-roll
  swap, host-insert toggle, re-render. Project history. One-click export pack.

### Phase 6: Learning loop and polish
- YouTube Data/Analytics OAuth (the user grants access through OAuth; we never ask for
  passwords), optional vidIQ; features from the last 2-3 days of uploads → picker + title
  context; "why this clip" panel.
- Scheduling/posting helpers, error handling, resumable renders, deploy guide, top-10 review.

## 5. Risks and honest limits

- **Diarization on crosstalk** is the weakest link for host/guest logic. Mitigation: confirm in
  the UI, and use per-speaker mouth movement as a second signal.
- **Stock B-roll rarely matches specific real events.** WEAK flags + the swap screen are the
  real answer, not more automation.
- **60 fps output from 30 fps sources** means frames are duplicated (or interpolated). Default:
  duplicate frames (no artifacts); interpolation optional.
- **Re-transcription diff** can fail on names that whisper mis-hears; diff against
  normalized text and allow a configurable tolerance per word.
- **GPU availability** decides whisper speed; the CPU path works but is slow on long episodes.

## 6. Product decisions needed from the owner

See the "Open questions" list in the reply that came with this plan. Defaults are proposed for
each, so "go" means accepting the defaults.
