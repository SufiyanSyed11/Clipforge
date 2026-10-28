"""`clipforge-golden`: run Phase 1 on the owner's three golden-sample episodes.

    clipforge-golden init      # writes golden/episodes.json to fill in
    clipforge-golden run       # runs every episode (or --episode ID)

Results go to docs/reports/phase-1/<episode id>/ (transcript, clips, QA) plus summary.md.
Nothing is written as "passed" by this tool: it records what ran and what the QA checks said,
and leaves the "what is weak" review to the owner. It refuses to run with FAKE_MODELS.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from clipforge.db import Project, session_scope, utcnow
from clipforge.jobs.registry import JobContext
from clipforge.pipeline import tasks
from clipforge.settings import REPO_ROOT, get_settings

EPISODES = REPO_ROOT / "golden" / "episodes.json"
REPORT_DIR = REPO_ROOT / "docs" / "reports" / "phase-1"

TEMPLATE = {
    "_how_to": (
        "Add exactly three episodes you have the rights to clip. 'source' is a local file path "
        "or a URL. Set rights_confirmed to true only if you have the rights or permission. "
        "Try to include one with noisy audio or crosstalk and one multi-camera show. "
        "host_speaker is optional (e.g. 'S1'); leave it null to use the app's suggestion."
    ),
    "episodes": [
        {
            "id": "ep1",
            "title": "",
            "source": "",
            "rights_confirmed": False,
            "notes": "e.g. clean studio audio",
            "host_speaker": None,
        },
        {
            "id": "ep2",
            "title": "",
            "source": "",
            "rights_confirmed": False,
            "notes": "e.g. noisy audio / crosstalk",
            "host_speaker": None,
        },
        {
            "id": "ep3",
            "title": "",
            "source": "",
            "rights_confirmed": False,
            "notes": "e.g. multi-camera with a TV wall",
            "host_speaker": None,
        },
    ],
}


class _PrintContext(JobContext):
    def log(self, message: str, progress: float | None = None, level: str = "info") -> None:
        print(f"  {message}")

    def heartbeat(self) -> None:
        pass


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


def run_episode(ep: dict, n: int) -> dict:
    out = REPORT_DIR / ep["id"]
    out.mkdir(parents=True, exist_ok=True)
    src = ep["source"]
    is_url = src.startswith(("http://", "https://"))
    with session_scope() as s:
        p = Project(
            name=f"golden:{ep['id']} {ep.get('title', '')}".strip(),
            source_type="url" if is_url else "upload",
            source=src if is_url else str(Path(src).expanduser().resolve()),
            rights_confirmed=bool(ep.get("rights_confirmed")),
            rights_confirmed_at=utcnow() if ep.get("rights_confirmed") else None,
            clip_count=n,
        )
        s.add(p)
        s.flush()
        pid = p.id
    ctx = _PrintContext(job_id=0, kind="golden", payload={}, project_id=pid)
    started = time.time()
    t, qa = tasks.run_ingest_and_transcribe(pid, ctx)
    transcribe_s = time.time() - started
    with session_scope() as s:
        p = s.get(Project, pid)
        suggestion = p.host_suggestion
        host = ep.get("host_speaker") or (suggestion or {}).get("speaker")
        p.host_speaker, p.host_confirmed = host, True
        speakers = p.speakers
        media_info = p.media_info
    host_source = (
        "set in episodes.json" if ep.get("host_speaker") else "app suggestion (unreviewed)"
    )
    pick_result = tasks.pick_clips_task(ctx)
    pdir = Path(tasks.get_storage().project_dir(pid))
    clips = json.loads((pdir / "clips.json").read_text(encoding="utf-8"))
    for name in ("transcript.json", "transcript.txt", "clips.json"):
        (out / name).write_bytes((pdir / name).read_bytes())
    all_qa = [r.to_dict() for r in qa] + [r.to_dict() for r in pick_result.qa]
    (out / "qa.json").write_text(json.dumps(all_qa, indent=2), encoding="utf-8")
    return {
        "id": ep["id"],
        "title": ep.get("title"),
        "notes": ep.get("notes"),
        "project_id": pid,
        "duration_min": round(t.duration / 60, 1),
        "transcribe_seconds": round(transcribe_s),
        "words": len(t.words),
        "speakers": speakers,
        "media_info": media_info,
        "host": host,
        "host_source": host_source,
        "host_suggestion": suggestion,
        "clips": clips["clips"],
        "rejected": clips["rejected"],
        "qa": all_qa,
    }


def summary_markdown(results: list[dict], env: dict | None) -> str:
    s = get_settings()
    lines = [
        "# Phase 1 golden run",
        "",
        f"Run at {datetime.now().isoformat(timespec='minutes')}. "
        f"Picker model `{s.picker_model}`, prompt `{s.picker_prompt_version}`.",
    ]
    if env:
        w = env["defaults"]["whisper"]
        lines.append(
            f"Machine: {env['os']}, {env['cpu_cores']} cores, {env['ram_gb']} GB RAM, "
            f"GPU: {', '.join(g['name'] for g in env['gpus']) or 'none'}. "
            f"Whisper {w['model']} on {w['device']}."
        )
    for r in results:
        lines += ["", f"## {r['id']}: {r.get('title') or ''}", ""]
        if r.get("error"):
            lines.append(f"**Failed:** {r['error']}")
            continue
        lines.append(
            f"{r['duration_min']} min, transcribed in {r['transcribe_seconds']} s, "
            f"{r['words']} words. Notes: {r.get('notes') or '-'}"
        )
        stats = (r.get("speakers") or {}).get("stats", [])
        lines.append(
            "Speakers: "
            + ", ".join(f"{x['speaker']} {x['talk_share']:.0%}" for x in stats)
            + f". Host: {r['host']} ({r['host_source']})."
        )
        failing = [
            f"{rep['step']}: {c['name']} ({c['detail']})"
            for rep in r["qa"]
            for c in rep["checks"]
            if not c["passed"]
        ]
        lines.append("QA items to look at: " + ("; ".join(failing) if failing else "none"))
        lines += ["", "| # | Window | Length | Score | Hook | Flags |", "|---|---|---|---|---|---|"]
        for c in r["clips"]:
            p = c["plan"]
            window = " + ".join(f"{_fmt(x['start'])}-{_fmt(x['end'])}" for x in p["segments"])
            flags = "; ".join(c["flags"] + [f"{f['type']}" for f in p["risk_flags"]]) or "-"
            hook = p["hook"].replace("|", "/")[:90]
            lines.append(
                f"| {c['rank']} | {window} | {c['duration']:.1f} s | {p['virality_score']} | "
                f"{hook} | {flags} |"
            )
        if r["rejected"]:
            lines.append("")
            lines.append(
                "Rejected by the hard rules: "
                + "; ".join(f"#{x['model_rank']} {x['reason']}" for x in r["rejected"])
            )
        lines += ["", "### Owner review: what is weak", "", "- (fill in after watching/listening)"]
    lines += [
        "",
        "## Not verified by this run",
        "",
        "- Clip windows use whisper timestamps; cutting in real silence is Phase 2.",
        "- Clip quality is the owner's judgement; the QA checks only cover structure and rules.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    run = sub.add_parser("run")
    run.add_argument("--episode", help="run only this episode id")
    run.add_argument("-n", type=int, default=8)
    args = ap.parse_args(argv)

    if args.cmd == "init":
        if EPISODES.exists():
            print(f"{EPISODES} already exists; not overwriting it.")
            return 0
        EPISODES.parent.mkdir(parents=True, exist_ok=True)
        EPISODES.write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")
        print(f"Fill in {EPISODES}, then run: clipforge-golden run")
        return 0

    if get_settings().fake_models:
        print("FAKE_MODELS is on. Golden runs must use real models; set FAKE_MODELS=false.")
        return 2
    if not EPISODES.exists():
        print("No golden/episodes.json yet. Run: clipforge-golden init")
        return 2
    episodes = json.loads(EPISODES.read_text(encoding="utf-8"))["episodes"]
    if args.episode:
        episodes = [e for e in episodes if e["id"] == args.episode]
    ready = [e for e in episodes if e.get("source")]
    for e in ready:
        if not e.get("rights_confirmed"):
            print(f"{e['id']}: rights_confirmed is false; refusing to ingest it.")
            return 2
    if not ready:
        print("No episodes with a source in golden/episodes.json.")
        return 2

    from clipforge.tools.check_env import load_report

    results = []
    for e in ready:
        print(f"== {e['id']} {e.get('title') or ''}")
        try:
            results.append(run_episode(e, args.n))
        except Exception as ex:  # record and continue with the next episode
            print(f"  FAILED: {ex}")
            results.append({"id": e["id"], "title": e.get("title"), "error": str(ex)})
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "golden-summary.md").write_text(
        summary_markdown(results, load_report()), encoding="utf-8"
    )
    print(f"Wrote {REPORT_DIR / 'golden-summary.md'}")
    return 1 if any(r.get("error") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
