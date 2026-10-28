# Golden-sample episodes

The three real episodes ClipForge is judged on. Media files here are gitignored; only
`episodes.json` is meant to be committed.

1. `clipforge-golden init` creates `episodes.json`.
2. For each of the three entries, set `source` to a local file path or a URL, and set
   `rights_confirmed` to `true` only if you have the rights or permission to clip it. Aim for:
   one clean studio episode, one with noisy audio or crosstalk, one multi-camera show with a
   TV wall behind the speakers.
3. Optional: set `host_speaker` (for example `"S1"`) once you know which label is the host.
   Left `null`, the run uses the app's suggestion and the report says it was unreviewed.
4. Stop the worker if `scripts\dev.ps1` is running (the golden run drives the same job queue).
5. `clipforge-golden run` transcribes, picks clips and builds every clip through the full
   pipeline (cut, verification gate, framing, captions, B-roll, mix, render, copy). It writes
   `docs/reports/golden/<id>/` (transcript, clips, per-clip QA) and
   `docs/reports/golden/golden-summary.md` with a quality-bar table per clip.
   `--pick-only` runs just transcription and clip picking; `--episode ep2` runs one episode.
6. Open the app: the projects are named `golden:ep1` etc. Watch and listen to every clip
   (listen at every cut), review the B-roll contact sheets, then fill in "what is weak" for
   each episode in the summary.

The quality bar is met only when every clip shows PASS on every row **and** your review found
nothing wrong. The tool never writes "passed" for anything it can't measure.
