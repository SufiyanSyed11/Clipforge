# Phase 4 report: B-roll engine

Status: **built; automated criteria pass with synthetic footage, simulated stock APIs and a
test-double vision model.** No real stock footage was downloaded and no real vision model ran
here (the sandbox blocks Pexels, Pixabay and Ollama's registry). Real-world B-roll quality is
unverified.

## What was built (`backend/clipforge/broll/`)

- **Beats** (`beats.py`):
  - One new visual every 2-3 s, cut on word boundaries.
  - Each beat is 62 % B-roll, then back to the speaker.
  - The hook (first sentence, at least 1.2 s) and the last second (punchline) stay on the
    speaker.
  - Never over the host-reaction insert; frame-grid aligned.
- **Search terms** (`queries.py`): beat-specific queries from the configured language model
  (local Qwen3 by default), plus any named person or event, schema-validated. If the model
  isn't available, a keyword fallback is used and logged as such.
- **Sources** (`sources.py`):
  - Pexels and Pixabay (free API keys). Only files whose short side is at least 1080 px; the
    smallest such file is downloaded.
  - Permitted YouTube footage per project: every URL needs rights confirmed, and the podcast's
    own URL is refused.
  - The owner's library (`data/broll_library`, optional JSON sidecars with title, tags,
    licence).
  - The owner's manual uploads per slot.
- **Media** (`media.py`):
  - Cached downloads with a 400 MB cap.
  - A scene-cut-free window: frames sampled at 4 fps, picked from the middle of the longest
    stretch without a cut.
  - A 3×2 contact sheet per candidate.
  - Perceptual hashes of the podcast (0.5 fps).
- **Checks** (`engine.py`, `vision.py`):
  - **Never podcast footage:** refused if the URL matches the podcast, the file hash matches,
    or any contact-sheet frame's perceptual hash is within 6 bits of a podcast frame.
    `podcast_checked` is recorded on every chosen shot, and QA verifies it.
  - **Vision check** (optional model): the model lists what it sees, covering the reject rules
    (text, captions, watermarks, logos, score bugs, lower-thirds, black bars, wrong
    person/event, soft footage, scene cuts, podcast footage) and the owner's banned-content
    list (Settings; defaults from the spec). It also says whether the footage matches the
    line. Code decides: any issue, any banned item or a mismatch means no.
  - **Named people:** the name must appear in the footage's own title or tags. The model is
    asked for context consistency only, never to recognise faces. Otherwise the shot is
    flagged "unverified identity" and never used automatically.
  - **Rules hash:** every approval records which rule set (reject rules + banned list + vision
    model) it was checked against. `reverify()` (used by the export gate in Phase 5) fails any
    shot approved under different rules.
- **No good footage:** the slot is `WEAK: needs manual cover` with the reason: no footage
  found, nothing passed, or no vision model (approve a candidate). Candidates are kept for
  review.
- **Without a vision model (the default)**: nothing is auto-accepted. Candidates are prepared
  with contact sheets and the owner approves them on the review screen.
- **Review screen** (clip editor): every slot's chosen contact sheet, its candidates (with
  vision issues and flags), "Use this", "Stay on speaker", "Automatic", and "Upload my own
  cover". Changes apply on the next build.
- **Clip editor page** (also the start of Phase 5): render preview, QA checklists, waveform
  with draggable trim handles (snapped to silence on rebuild), caption word fixes, host-insert
  toggle.

## Evidence

| Test | Result |
|---|---|
| `tests/test_broll.py` (12): beat spacing/share/hook/end/grid; host insert avoided; queries from the model or a labelled keyword fallback; Pexels and Pixabay parsing with the 1080p filter (simulated APIs); a clip with a scene cut gets a window that avoids it; watermark footage rejected by vision; 720p filtered; no sources gives WEAK; **footage whose frames match the podcast is rejected**; owner approval without vision; **changing the banned list invalidates the approval**; clearing a slot; named person not in the metadata is never used | **All pass** |
| `tests/test_api_broll.py` (2): library footage fills slots in a real build, the render passes its checks with cutaways, contact-sheet endpoint, clearing a slot then rebuilding, validation of approvals, permitted sources need rights and can't be the podcast | **Pass** |
| Full backend suite | **152 passed** |
| Browser tests | **3 passed** |

## What is weak / not verified

- **No real stock footage, real API responses or real vision model** were used. API parsing
  follows the public Pexels/Pixabay response formats; it needs a run with real keys.
- **A small local vision model (if enabled) may miss small text or logos.** Without one, the
  owner is the check, which is safest but manual.
- Stock B-roll rarely matches specific real events (as the spec warns); expect many WEAK slots
  for named people and specific moments. The review screen and manual covers are the answer.
- B-roll decisions are keyed by slot number; if a trim changes the beats, earlier decisions may
  point at a different slot (the review screen shows the result).
- Downloads are cached in `data/broll_cache` and can grow large; Phase 6 adds disk cleanup.
