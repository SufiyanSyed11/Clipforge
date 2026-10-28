# ClipForge — project rules for Claude Code

ClipForge turns a podcast (YouTube URL or uploaded file) into ready-to-post, viral-grade
vertical shorts (YouTube Shorts / Reels / TikTok / X). The output must look hand-directed by a
pro editor, not like an automated template. The owner is a video editor and social media
manager: judge every output like a viewer and like an editor.

Source of truth for the full spec: `docs/SPEC.md`. Architecture and phased plan:
`docs/ARCHITECTURE.md`. This file holds the rules that must never be forgotten.

## Owner decisions (see docs/ARCHITECTURE.md section 0)

- Local Windows PC first, Docker optional. Pick GPU-dependent components only from what
  `clipforge-check` reports.
- Host is auto-suggested, owner confirms/changes with one click per project.
- 30 fps sources go to 60 fps by frame repetition; frame interpolation only if enabled.
- Brand tag default `#DoubleCoverage`; logo uploaded in Settings.
- Never invent test results. The quality bar is passed only after the owner's 3 golden
  episodes actually ran. A phase is complete only when its acceptance criteria pass.
- B-roll restrictions are Settings with the spec defaults, applied to every selection and
  re-verified before export.

## Working rules

- Plan before coding. Build phase by phase. After every phase: run it on a real sample
  podcast, show the output, list what is weak and what was NOT verified, fix it, then move on.
- Never claim something works until a real run proves it. Be honest about weak results.
- Don't ask for simple permissions; take the obvious next step. Ask only real product decisions.
- Never delete or overwrite the owner's existing files without asking. New output goes into
  new, clearly named folders.
- Config lives in `.env` (see `.env.example`). No secrets in the repo. Never ask users for
  cookies or passwords.
- The user must confirm they have rights/permission to clip content before anything is
  downloaded.
- Every pipeline step is its own module, a pure testable function, with tests, and returns a
  machine-readable QA report that the UI renders as a checklist.

## Hard style rules (default preset, editable in the UI)

- 1080x1920, 60 fps (frame repetition from 30 fps by default). Full-screen vertical, face-tracked camera + B-roll cutaways.
- Clip length 18-30 s, **hard max 30 s**. Hook inside the first second. End on the punchline so
  it loops. A clip is a complete mini-story (opening -> hook -> context -> build -> payoff).
- One-word-at-a-time centered bold captions, emphasis on key words, timed to corrected words.
  Logo/watermark top right. Respect each platform's safe zones.
- Slow push-in zoom, one consistent colour grade, no cheap effects, every transition motivated
  (clean cut by default). SFX only on motivated transitions, never decoration.

## Zero-clipped-words rules (the cut-point engine)

- Whisper word timestamps are off by 0.1-0.4 s in loud/noisy audio. **Never cut at a whisper
  timestamp.** For every boundary compute a 10 ms RMS trace and cut at the minimum-RMS point
  of true silence between neighbouring words.
- Noisy audio (floor 100-400): judge by RMS dips, not word ends. No real gap -> extend to the
  next natural pause; never cut inside speech.
- Snap cuts to the frame grid: `snap(t) = round(t * FPS) / FPS`. Keep A/V in sync. Tiny audio
  fades at joins.
- Pause removal only in true silence, strict RMS threshold + safety margin. The last word must
  finish and decay naturally.
- Profanity: prefer a different seam. Auto-bleep (mute + 1 kHz tone, censored caption `f**k`)
  only when no suitable alternative exists. Never damage speech to avoid a word.
- Ingest downloads the full episode as **one muxed file**. Never join separately downloaded
  audio and video (causes drift).

## Verification gate (no export unless all pass; fail loudly with the exact timestamp)

- Re-transcribe the cut audio and diff against the intended text.
- RMS check 40-60 ms before and after every join.
- First 0.3 s and last 0.45 s show natural onset and decay.
- Audio duration == video duration; frame count matches.

## Audio mix

- Voice normalized to about **-14 LUFS**, always clearly audible.
- Royalty-free BGM about **19 dB under the voice**, sidechain ducked. Limiter at **-1 dB**.

## Framing and host reaction

- Detect faces, follow the active speaker, smooth the camera path. Multi-camera shows: reject
  face detections outside the speaker region (TV walls with extra faces).
- If the host is not on camera during a clip, insert 1.3-2 s of the host listening (lips not
  moving; frame-difference + mouth-movement check), a different shot per clip. Audio untouched.

## B-roll (quality over quantity)

- One new visual every 2-3 s; each beat ~62% B-roll, then back to the speaker.
- Sources: Pexels, Pixabay, permitted YouTube footage. 1080p+ only. Beat-specific queries.
- Vision-model rejection on contact sheets: on-screen text, captions, watermarks, logos, score
  bugs, lower-thirds, black bars, wrong person/event, soft/low-res, scene cuts inside the
  window, and anything in the configurable "banned content" list.
- Named people: verify identity from title and visuals; never use a stand-in without flagging.
- No good footage -> mark the slot `WEAK: needs manual cover`. Never fill with junk.
- **Never use the podcast footage itself in a B-roll slot.**

## Copy pack per clip

- Title: curiosity gap, named person, quoted punchline, one emoji.
- YouTube description with emojis and a call to comment.
- Instagram caption with brand tag + related tags (max 5 tags total).
- X post: 1-2 sentences, no emojis. No credit line unless the user adds one.

## Golden-sample quality bar (3 real podcasts)

Zero clipped words, zero profanity leaks, zero text/logos in B-roll, voice at target loudness,
BGM under the voice, audio duration == video duration. Also test hard audio: crowd noise,
crosstalk, quiet guests, multi-camera with TV walls.
