# Phase 2 report: cut-point engine and verification gate

Status: **built; all automated criteria pass on synthetic audio. Real-podcast validation (the
golden episodes) is pending on the owner's PC.** Nothing here measures performance on real
speech yet.

## What was built

- **`core/audio.py`**: 10 ms RMS traces, local floor/speech levels, a silence threshold that
  follows the noise floor (dips relative to the floor, not an absolute level), silent-run
  finding, frame-grid snapping (`snap(t) = round(t·FPS)/FPS`), loudness measurement.
- **`core/cut.py`, the cut engine:**
  - Cuts go only inside measured silent runs of at least 140 ms, at the RMS minimum. They are
    on the 1/60 s grid and keep at least 60 ms of silence on each side, so the gate's
    "RMS 40-60 ms before/after every join" check passes by design.
  - Whisper's timing drift is estimated per region by cross-correlating its word layout with
    the measured speech energy (smoothed so pauses, not word rhythm, drive the match).
  - Order guard: a start cut never lands after its first word, nor an end cut before its last
    word. When unsure, the engine extends to the next natural pause: the clip may gain words,
    never silently lose one.
  - No real gap within reach: extend up to 3 s to the next natural pause, else fail with the
    exact timestamp.
  - Lead-in at most 0.2 s before the first word (hook lands fast). Tail at most 0.4 s after
    the last word (natural decay, inside the 0.45 s check).
  - Long pauses (over 0.45 s, 3 dB stricter threshold) are shortened, keeping 120 ms each side.
  - Profanity (owner decision 3):
    - A profane word in an interior sentence: that sentence is skipped with two silence cuts,
      if the clip stays at or above the minimum length.
    - In the hook or payoff, or when skipping isn't clean: only the word is bleeped. Its
      extent comes from the RMS dips around it, never past a neighbour's middle; the speech is
      muted under a 1 kHz tone and the caption reads `f**k`.
    - Modes: prefer-alternative (default), bleep, allow.
  - The 30 s hard max is re-checked after cutting; an over-long clip is refused, never
    silently trimmed.
  - Trim handles snap to the nearest real pause in either direction.
- **`core/voice.py`**: sample-exact assembly with 8 ms fades at every join and edge, and the
  bleeps.
- **`core/verify.py`, the gate:**
  - RMS 40-60 ms either side of every join.
  - First 30 ms silent plus speech within 0.3 s; last 50 ms silent.
  - Re-transcription diff against the words the clip is meant to contain: any intended word
    missing near a join or edge fails, with its output time. Extra words are a warning.
  - On the rendered file: exact frame count, audio duration equal to video duration within
    one AAC frame plus one video frame, and resolution.
- **`core/render.py`**: the renderer. It decodes each source window at 60 fps by frame
  repetition (interpolation only if enabled), builds every frame (camera crop, B-roll and the
  host insert are wired for Phases 3-4), and encodes with NVENC when it works, else libx264.
  Atomic output; the frame count is enforced.
- **`build_clip` job**: queued automatically for every picked clip, and re-run after edits.
  Clip status is `ready` only when every gate check passes, otherwise `needs_review` with the
  failing checks and timestamps.
- **Job cancellation and retry**: the API cancels queued jobs immediately and stops running
  ones at their next check (the render loop checks every 30 frames). Retry re-queues failed or
  cancelled jobs, and cached intermediates are reused.
- **Database upgrade in place**: new columns are added to an existing database automatically.
- **UI**: each clip card shows the rendered video, its status, the cut/verify/render QA, and
  Rebuild/Edit buttons.

## Evidence (automated, synthetic audio)

Synthetic "speech": tone-burst words with known true extents. Whisper-like word times drift
by up to ±0.4 s plus jitter. Noise floors go from studio-clean to int16 RMS 400 (the spec's
noisy range). A test-double transcriber reports a word only if (nearly) all of its burst is in
the cut audio.

| Test | Result |
|---|---|
| `tests/test_cut.py` (25 tests: clean, noisy, no-gap extension, no-pause failure, pause removal, stitched seams, profanity skip/bleep/modes, 30 s refusal, trims, sabotaged clipped word and join caught by the gate, sample-exact assembly, a 54-case stress grid) | **All pass** |
| Stress: 480 random sentence-aligned clips (4 noise floors × 3 whisper error levels × 40 seeds) | **480/480: no cut inside a word, every intended word heard, every gate check passed.** Unneeded extensions: 0/40 at 0.1 s error, 1/40 at 0.25 s, 2-8/40 at 0.4 s (the clip gains a few words; nothing is lost) |
| Stress: 240 random mid-sentence requests (start/end inside a sentence, so the engine must extend) | **No cut inside a word in any case.** 232-234/240 contain every intended edge word; in the remaining 6-8 (all at ±0.25-0.4 s whisper error) the first word was dropped, and **the gate caught every one** (clip marked needs-review, never exportable) |
| `tests/test_api_phase1.py`: upload → transcribe → host → pick → build every clip → gate; trim edit + rebuild; cancel + retry | **Pass** |
| Browser: full flow to rendered clip videos | **Pass** |
| Render check on a 20.4 s clip | 1225/1225 frames; audio vs video duration diff 1 ms |

## What is weak / not verified

- **No real speech has been cut yet.** The synthetic tests prove the engine's logic and the
  gate's ability to catch clipped words. Real voices (breaths, music beds, laughter, crosstalk)
  are the golden-episode test.
- **The real re-transcription (faster-whisper on the cut audio) has not run here** (no model
  download possible in the sandbox). With test doubles, the gate correctly reports
  "re-transcription by a real model: failed", so no clip can be marked verified.
- **Large whisper errors on mid-sentence boundaries**: about 3 % of such synthetic cases
  start one word late. The gate catches them, but the owner then sees "needs review" and has
  to trim. Real whisper errors are usually smaller; to be measured on the golden episodes.
- Very long single pauses or music beds under speech may confuse the floor estimate; untested.

## Next

Phase 3 fills the parts of the spec the renderer already has slots for: face-tracked camera
path and push-in, the host-reaction insert, one-word captions, and the −14 LUFS mix with BGM
ducking and the −1 dB limiter.
