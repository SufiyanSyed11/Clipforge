"""`clipforge-picker-harness`: run clip-picker prompt versions on a saved transcript and
compare them side by side.

    clipforge-picker-harness data/projects/00001/transcript.json --prompts v1 v2 --host S1
    clipforge-picker-harness transcript.json --compare runs/v1.json runs/v2.json   # no API call

Writes one JSON per prompt version plus compare.md into --out.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from clipforge.core import pick
from clipforge.models import Transcript
from clipforge.settings import get_settings


def run_version(
    transcript: Transcript,
    version: str,
    host: str | None,
    n: int,
    client: pick.PickerClient,
) -> dict:
    result = pick.pick_clips(
        transcript, host, client, lambda m: print(f"  [{version}] {m}"), n=n, prompt_version=version
    )
    qa = pick.pick_qa(result, n, transcript)
    return {
        "prompt_version": version,
        "host_speaker": host,
        "n": n,
        "fake": transcript.fake or isinstance(client, pick.FakePickerClient),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "clips": [c.model_dump() for c in result.clips],
        "rejected": result.rejected,
        "qa": qa.to_dict(),
    }


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:05.2f}"


def compare_markdown(runs: list[dict]) -> str:
    lines = ["# Clip picker comparison", ""]
    for r in runs:
        tag = " (FAKE stub, not a real result)" if r.get("fake") else ""
        lines.append(
            f"- **{r['prompt_version']}**{tag}: {len(r['clips'])} clips, "
            f"{len(r['rejected'])} rejected, host {r.get('host_speaker')}"
        )
    lines += ["", "| Rank | " + " | ".join(r["prompt_version"] for r in runs) + " |"]
    lines.append("|---|" + "---|" * len(runs))
    depth = max((len(r["clips"]) for r in runs), default=0)
    for i in range(depth):
        cells = []
        for r in runs:
            if i < len(r["clips"]):
                c = r["clips"][i]
                p = c["plan"]
                window = " + ".join(f"{_fmt(s['start'])}-{_fmt(s['end'])}" for s in p["segments"])
                hook = p["hook"].replace("|", "/")[:80]
                cells.append(
                    f"{window} ({c['duration']:.1f} s, score {p['virality_score']}) {hook}"
                )
            else:
                cells.append("—")
        lines.append(f"| {i + 1} | " + " | ".join(cells) + " |")
    # Clips both versions agree on (>50% overlap).
    if len(runs) == 2:
        a, b = runs
        same = 0
        for ca in a["clips"]:
            pa = pick.ClipPlan.model_validate(ca["plan"])
            if any(
                pick._overlap(pa, pick.ClipPlan.model_validate(cb["plan"])) > 0.5
                for cb in b["clips"]
            ):
                same += 1
        lines += ["", f"Clips found by both versions: {same}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--prompts", nargs="+", default=["v1"], help="prompt versions to run")
    ap.add_argument("--host", help="host speaker label, e.g. S1")
    ap.add_argument("-n", type=int, default=8)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--compare", nargs="+", type=Path, help="compare saved run JSONs only")
    ap.add_argument("--fake", action="store_true", help="use the stub picker (plumbing test)")
    args = ap.parse_args(argv)

    out = args.out or Path("data/harness") / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    if args.compare:
        runs = [json.loads(p.read_text(encoding="utf-8")) for p in args.compare]
    else:
        transcript = Transcript.model_validate_json(args.transcript.read_text(encoding="utf-8"))
        settings = get_settings()
        runs = []
        for version in args.prompts:
            client = (
                pick.FakePickerClient(transcript)
                if args.fake
                else pick.ClaudePickerClient(
                    settings.picker_model,
                    settings.picker_effort,
                    settings.picker_fallbacks,
                    api_key=settings.anthropic_api_key or None,
                )
            )
            try:
                run = run_version(transcript, version, args.host, args.n, client)
            except pick.PickerError as e:
                print(f"{version}: {e}", file=sys.stderr)
                return 1
            (out / f"{version}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
            runs.append(run)
    (out / "compare.md").write_text(compare_markdown(runs), encoding="utf-8")
    print(f"Wrote {out / 'compare.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
