# Phase 6 report: learning loop, posting, storage, security, final hardening

Status: **built; automated criteria pass with test doubles and mock transports.** The YouTube
connection, real uploads and every quality judgement need the owner's PC and accounts.

## What was built

- **Learning loop** (`core/learn.py`, `core/youtube.py`, `api/learning.py`, Learning page):
  - Data from your channel: YouTube OAuth (PKCE, loopback redirect, read-only by default), or
    a YouTube Studio / vidIQ CSV export.
  - Each recent upload is scored by views per hour, weighted by average % viewed. The top half
    is compared with the bottom half on length, title patterns (one emoji, quoted punchline,
    question, number/money, named person, short title), topic words and upload hour.
  - The summary goes into the picker and copy-writer prompts as a tie-breaker that never
    overrides the clip rules. It is only used while fresh (7 days) and with 2+ uploads.
  - Clip cards show "Channel data: …" notes explaining which insights a clip matches.
  - Date-only CSVs don't invent a best upload hour (found and fixed during this phase).
- **Posting** (`core/posting.py`, editor "Posting" section):
  - Suggested posting time: your best recent hour, else 18:00 local.
  - `.ics` calendar download and a `posting.md` checklist in the export pack.
  - Optional upload to YouTube, **private with a scheduled publish time** (verified clips with
    a copy pack only, at least 15 minutes ahead). It needs the upload permission, which is
    off by default.
- **Storage** (`core/disk.py`, System → Storage):
  - Usage breakdown.
  - Downloads, audio extraction and renders refuse to start below `MIN_FREE_GB`.
  - Cleanup of re-creatable files (extracted audio, voice/mix stems) and the B-roll cache;
    sources, renders and export packs are never touched.
  - Project delete with explicit confirmation; the job history is kept.
- **Security:**
  - Host-header allowlist (blocks DNS rebinding) and an `X-ClipForge` header required on
    every write.
  - Upload size limit, OAuth callback escaping, JSON errors without stack traces.
  - Keys never sent to the UI; token file with owner-only permissions.
- **Verification gate:** new "no profanity audible in the cut" check on the re-transcription,
  so a profanity leak blocks export (a warning only if the profanity setting is "allow").
- **6 GB GPU fix:** clip builds now unload Ollama's text and vision models before the gate
  runs Whisper on the GPU. Before, only episode transcription did, so a build could run out
  of VRAM after B-roll queries.
- **Golden tool:** `clipforge-golden run` now builds every clip through the real job queue
  and writes a per-clip quality-bar table (`docs/reports/golden/`).
- **Dependencies:**
  - Next.js 14.2.15 → 16.4.3 and React 18 → 19.1. 14.2.x has unpatched critical advisories,
    including remote code execution on Windows-hosted servers; even 14.2.35 still audits
    critical.
  - ESLint 9 with the Next config, now run in CI.
  - Playwright 1.47 → 1.64 (fixes an advisory on browser downloads).
  - Fixes for the stricter React 19 hook rules.
- `.env.example` documents every setting; the setup scripts check Node 20.9+.
- `docs/DEPLOY.md`: what runs where on the RTX 3050 and on CPU, a measured render time, disk
  budget, security model, cloud path, troubleshooting.

## Evidence

| Test | Result |
|---|---|
| `tests/test_phase6.py` (22): insights (patterns, best length, freshness, too little data, date-only CSV), Studio CSV parsing (BOM, Total row, quoted thousands), OAuth PKCE/state, token exchange and refresh, token file `0600`, recent uploads with analytics (mock transport), private scheduled upload body, suggested time, ICS escaping, learning API guards and CSV import, YouTube schedule validation, calendar download, posting plan, cleanup keeps sources/renders, low-disk refusal, storage API including confirmed project delete, **Ollama text+vision unloaded before GPU gate Whisper (not on CPU)** | **All pass** |
| `tests/test_security.py` (7): writes without the header refused, DNS-rebinding host refused, CORS only for the web app, keys never in API output, callback escaping, files only from inside the project, upload size limit | **All pass** |
| `tests/test_cut.py`: gate fails on audible profanity, passes (as a warning) when allowed | **Pass** |
| `tests/test_tools_phase1.py`: quality-bar mapping; **golden episode through the full pipeline** (pick → analyse → build → copy) with test doubles, never verified | **Pass** |
| Full backend suite | **197 passed** |
| Browser (5, serial): ping, rights gate + clips, URL rights refusal, **learning CSV → insights, storage cleanup**, full workflow to export | **All pass** (screenshots below) |
| `ruff check`, `ruff format --check`, `npm run lint`, `npm run typecheck`, `npm run build` | **Clean** |
| `npm audit` (all deps), `pip-audit` | **0 known vulnerabilities** |

Screenshots: `learning.png`, `system-storage.png`, `editor-e2e.png` (Posting section, new
gate check).

## What is weak / not verified

- **No real YouTube account has been connected.** The OAuth and API calls follow Google's
  documented formats and are tested against a mock transport only. Google may require the
  OAuth app to be in "testing" mode with your account added as a test user.
- **No real upload has been made.** Scheduled publishing needs a verified YouTube channel.
- Insights from a handful of uploads are noisy (4 uploads give ±100-point "lifts"). They are a
  tie-breaker only, and the panel shows the number of uploads behind them.
- `setup.ps1` changes (Node version check) were not run on Windows (no PowerShell here).
- **Python type checking isn't enforced.** `mypy` reports 83 findings; the sampled ones are
  variable reuse and Optional narrowing, not runtime bugs. TypeScript is fully typechecked.
- The browser tests now run serially. In parallel they shared one job worker and timed out
  on queue contention; the app itself was fine.
