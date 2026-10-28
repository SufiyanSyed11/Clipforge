# Golden-sample episodes

The three real episodes Phase 1 (and later phases) are judged on. Media files here are
gitignored; only `episodes.json` is meant to be committed.

1. `clipforge-golden init` creates `episodes.json`.
2. For each of the three entries, set `source` to a local file path or a URL, and set
   `rights_confirmed` to `true` only if you have the rights or permission to clip it. Aim for:
   one clean studio episode, one with noisy audio or crosstalk, one multi-camera show with a
   TV wall behind the speakers.
3. Optional: set `host_speaker` (for example `"S1"`) once you know which label is the host.
   Left `null`, the run uses the app's suggestion and the report says it was unreviewed.
4. `clipforge-golden run` writes `docs/reports/phase-1/<id>/` (transcript, clips, QA) and
   `docs/reports/phase-1/golden-summary.md`.
5. Listen and watch, then fill in "what is weak" for each episode in the summary.
