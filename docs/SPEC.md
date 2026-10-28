# ClipForge spec

Copied from the owner's "ClipForge: Claude Code build prompts" Google Doc. This is the product
spec; `CLAUDE.md` holds the non-negotiable rules extracted from it.

## Product (what the user does)

- Paste a YouTube link or drop a file. Pick a style preset. Click Generate.
- The app returns N ranked clips (default 8). Each clip card shows: preview player, virality
  score with reasons, title, YouTube description, Instagram caption, X post, hashtags, and a
  "QA passed" checklist.
- Every clip is editable: trim handles that snap to silence, swap any B-roll shot, edit caption
  words, toggle the host-reaction insert, re-render. Export an MP4 plus a pack folder (video,
  transcript, shot list, captions file, copy).
- Project history and a per-channel "what worked" panel (learning loop).

## Pipeline (each step is its own module with tests)

1. **Ingest** — yt-dlp for permitted sources (user confirms rights), ffprobe, full-episode
   download as one muxed file.
2. **Transcribe** — faster-whisper with word timestamps + speaker diarization (guest vs host).
3. **Clip picking** — Claude API with the clip-picker system prompt below. Strict JSON:
   segments with exact episode windows, hook, context, payoff, virality score 1-10 with
   reasons, title options, risk flags (profanity, defamation, private info, unverifiable
   claims).
4. **Cut-point engine** — RMS-silence cuts, frame-grid snapping, pause removal, profanity
   handling (see `CLAUDE.md`).
5. **Verification gate** — re-transcription diff, join RMS checks, onset/decay checks, A/V
   duration and frame-count checks.
6. **Framing** — 1080x1920 @ 60 fps, active-speaker face tracking, smoothed camera path, slow
   push-in, multi-camera handling.
7. **Host-reaction insert** — 1.3-2 s listening shot when the host is off camera.
8. **B-roll engine** — beats every 2-3 s, stock + permitted footage, vision-model rejection,
   honest WEAK flags, never podcast footage.
9. **Captions** — one word at a time, corrected words, emphasis, logo, platform safe zones.
10. **Audio mix** — voice -14 LUFS, BGM -19 dB under with sidechain ducking, limiter -1 dB,
    SFX only on motivated transitions.
11. **Copy pack** — title, YouTube description, Instagram caption (brand tag, max 5 tags),
    X post.
12. **Learning loop** — YouTube analytics (optionally vidIQ): read the last 2-3 days of
    uploads, find which hooks/lengths/topics/titles performed, feed into the picker and title
    writer, show "why this clip".

## Style preset (default, editable)

Full-screen vertical, face-tracked camera plus B-roll cutaways, one-word centered captions,
logo top right, slow push-in zoom, one consistent colour grade, clips 18-30 s (hard max 30 s),
ends on the punchline so it loops, hook inside the first second, no cheap effects, every
transition motivated (clean cut by default).

Owner's default banned-content list for B-roll: women, and any person who is not clearly
identified. This is a per-user setting, not a global rule.

## Tech choices (proposed, to confirm)

- Frontend: Next.js + TypeScript + Tailwind, dark UI, live job logs, video preview with
  waveform and word-level transcript.
- Backend: Python FastAPI + worker queue for ffmpeg, whisper, OpenCV/MediaPipe; Postgres +
  object storage. Every media step a pure, testable function.
- Models: Claude API for clip picking, copy, and vision QA of contact sheets. Local
  faster-whisper (GPU if available, CPU fallback with chunking).
- Rendering: ffmpeg filtergraphs first; Remotion only if it clearly improves captions/motion.
  Cache intermediates; every render resumable.

## Clip picker system prompt (used inside the app)

> You are a short-form video editor and viral strategist for podcast clips. Given a
> timestamped, speaker-labelled transcript, return the best clips as strict JSON.
>
> A clip must be a complete mini-story: natural opening -> hook -> context -> build-up ->
> escalation -> payoff.
> - Max length 30 s (target 18-28 s after pause removal). Never exceed 30 s.
> - Start at the earliest point that establishes who/what is being discussed. Never open on
>   "and / but / so / that's why / he / they" or any line that depends on earlier talk. If the
>   payoff needs it, include 10-20 s of earlier context.
> - Do not pick a clip just for a great quote. A quote with no setup is not a clip.
> - The first sentence (or a re-ordered line) must create curiosity or tension inside 1 second.
> - End on the punchline or the most surprising line so the clip loops. Do not end on a
>   trailing thought.
> - Prefer: named people, specific numbers, conflict, a surprising reversal, a funny twist, a
>   confession, money, status, fear, "I can't believe he said that".
> - Avoid: inside jokes, long pauses, rambling, unclear references, anything that needs the
>   full episode.
> - You may stitch several windows from different timestamps if the story stays coherent; mark
>   each seam and make sure no sentence is cut mid-thought.
> - Flag risks: profanity (list the words and times), claims about real people that are
>   unverifiable (label them "he says"), private information, defamation.
>
> For each clip return: segments [{start,end,text}], hook line, context line, payoff line,
> virality_score 1-10, 3 reasons, 3 title options (catchy, a quoted punchline, one emoji),
> B-roll beats (what real footage would show each line), risk flags, and what could make it
> better.
>
> Rank clips best first. If fewer than N good clips exist, return fewer. Never pad.

Implementation note from the owner: use this as the system prompt for the clip-picking step,
validate its JSON with a schema, and build a small test harness to run it on any transcript and
compare prompt versions.

## Phases

1. **Ingest, transcript, clip picking** — yt-dlp + upload, faster-whisper with words and
   speakers, clip picker with strict JSON, simple UI listing ranked clips. Test on 3 podcasts.
   No rendering yet.
2. **Cut-point engine and verification gate** — RMS tools, frame-grid snapping, pause removal,
   profanity handling, re-transcription diff. Prove zero clipped words with a join-by-join QA
   report. Highest-priority phase.
3. **Framing, captions, audio mix** — face-tracked 1080x1920, active speaker, push-in,
   one-word captions, logo, -14 LUFS voice, BGM -19 dB with ducking, host-reaction insert,
   60 fps. Show 3 finished clips and what looks off.
4. **B-roll engine** — beats, stock + permitted search, vision rejection on contact sheets,
   WEAK flags, contact-sheet review/swap screen.
5. **Copy pack, editor UI, export** — copy generation, per-clip editor (snap-to-silence trim,
   caption edits, B-roll swap, re-render), project history, export packs, QA checklist on
   every clip.
6. **Learning loop and polish** — YouTube analytics (+ vidIQ), scheduling/posting helpers,
   error handling, resumable renders, deploy guide, full review with top 10 improvements.
