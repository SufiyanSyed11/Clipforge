# Phase 5 report: copy pack, editor UI, export

Status: **built; automated criteria pass with test doubles.** Real copy from the local model and
real exports of verified clips need the owner's PC (Ollama + real Whisper so the gate can pass).

## What was built

- **Copy pack** (`core/copywriter.py`): the local model (Qwen3 4B by default) drafts; code
  enforces the spec:
  - title: exactly one emoji, at most 100 characters (curiosity gap, named person, quoted
    punchline asked of the model);
  - YouTube description: needs emojis and a call to comment;
  - Instagram: brand tag (`#DoubleCoverage`) first plus related tags, de-duplicated, at most 5
    in total; hashtags written inside the caption text are moved to the tag list;
  - X post: 1-2 sentences, no emojis, at most 280 characters;
  - no credit line unless one is set in Settings.

  A rule break is fed back to the model (up to 2 retries), then the job fails with the reasons.
  Owner edits are checked against the same rules. The channel-performance context from Phase
  6 is passed in.
- **Export** (`core/export.py`, job `export_clip`):
  - Gate: the clip passed the verification gate, every B-roll shot was checked against the
    current rules (`reverify`), and a copy pack exists. Otherwise the export is blocked, with
    the reasons (and timestamps) in the job.
  - Pack folder plus a `.zip`: `video.mp4`, `transcript.txt/.json`, `shot_list.csv` (speaker
    pieces, B-roll with source/URL/licence/approval, WEAK slots, host insert), `captions.srt`
    and `.ass`, `copy.md/.json`, `qa.json`, and `manifest.json` with SHA-256 hashes of every
    file.
  - Re-exporting a clip replaces only its own previous pack.
  - With `FAKE_MODELS` only, unverified clips export into a folder marked `UNVERIFIED-TEST`
    with `verified: false`. Real runs can't do that (tested).
- **Clip editor**:
  - Render preview and every QA checklist.
  - Waveform with draggable trim handles (snapped to silence on rebuild).
  - Caption word fixes; host-insert toggle; B-roll review.
  - Copy-pack editor with copy-to-clipboard and regenerate.
  - Export button, blocking reasons, zip download.
- **Project page**: clips refresh while building, "Export all verified clips", and the job
  history with cancel and retry.
- **Settings page**:
  - Style: size, fps, 30→60 mode, clip length capped at 30 s, default clip count, colour
    grade, profanity mode.
  - Captions: font, uppercase, emphasis colour.
  - Brand: tag, credit line, logo upload.
  - Audio: music bed and SFX uploads.
  - B-roll: banned content and reject rules.
  - Face recognition toggle, plus model and stock-key status (set or not set only).

## Evidence

| Test | Result |
|---|---|
| `tests/test_phase5.py` (13): copy rules (brand tag first, max 5, de-dupe, hashtags moved out of the caption), each rule break rejected, credit line only when set, retry with feedback, safe failure, export gate reasons with timestamps, **API flow: auto copy after build, invalid owner edit refused, valid edit saved, export pack with all files and a hashed manifest, UNVERIFIED-TEST label in test mode, export blocked for the same clip in a real run** | **All pass** |
| Browser: full workflow (settings → import → host → clips → editor → copy → export → download) | **Pass** (screenshot `editor-e2e.png`, test doubles) |
| Full backend suite | **165 passed** |

## What is weak / not verified

- **No real copy has been generated** (no local model in the sandbox). A 4B model may need
  several retries to follow every rule; failures are explicit, never silent.
- The "curiosity gap / named person / quoted punchline" title qualities are asked of the model
  but can't be checked mechanically; the owner reviews them.
- **No verified export exists yet**: that needs a clip that passes the gate with real Whisper
  on the owner's PC.
