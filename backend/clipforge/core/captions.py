"""One-word-at-a-time captions (ASS for burning in, SRT for the export pack).

Centred, bold, high contrast (white with a heavy black outline), key words emphasised in the
accent colour, placed inside every platform's safe zone (about 62 % down the frame: clear of
the top bar and the bottom caption/like UI on Shorts, Reels and TikTok). Timed to the cut's
words; the owner's caption fixes replace the transcript's text; censored words stay censored.
"""

from __future__ import annotations

import re
from pathlib import Path

from clipforge.editspec import CaptionWord, EditSpec
from clipforge.qa import QAReport

SAFE_Y = 0.62  # caption centre as a share of frame height
MAX_TEXT_W = 0.80  # longest word may use this share of the frame width
MAX_EMPHASIS_SHARE = 0.25
HIDE_AFTER_GAP = 0.6  # pauses longer than this show no caption

STOPWORDS = set(
    """a an the and or but so to of in on at for with from by as is are was were be been it
    this that these those i you he she we they me him her us them my your his our their not no
    do did does have has had just very really like what when where who why how there here then
    than too also into about over out up down off again once can will would could should""".split()
)
_NUMBERISH = re.compile(r"^\$?\d|^(million|billion|thousand|hundred|percent)$", re.I)


def clean_word(text: str, uppercase: bool) -> str:
    w = re.sub(r"^[\"'(\[]+|[\"'),.;:\]]+$", "", text.strip())
    return w.upper() if uppercase else w


def pick_emphasis(words: list[CaptionWord], hook: str = "", payoff: str = "") -> list[bool]:
    """Numbers, names and the strongest content words of the hook/payoff; at most 25 %."""
    key_terms = {
        re.sub(r"\W", "", w.lower())
        for w in (hook + " " + payoff).split()
        if len(re.sub(r"\W", "", w)) >= 6 and re.sub(r"\W", "", w.lower()) not in STOPWORDS
    }
    scores = []
    prev_end_punct = True
    for c in words:
        raw = c.text
        bare = re.sub(r"\W", "", raw)
        s = 0.0
        if _NUMBERISH.search(bare) or "$" in raw:
            s = 3.0
        elif bare[:1].isupper() and not prev_end_punct and bare not in ("I", "I'm", "Im"):
            s = 2.5  # a name mid-sentence
        elif bare.lower() in key_terms:
            s = 2.0
        elif bare.isupper() and len(bare) > 1:
            s = 1.5
        if c.censored:
            s = 0.0
        scores.append(s)
        prev_end_punct = bool(re.search(r"[.?!]$", raw))
    budget = max(1, int(len(words) * MAX_EMPHASIS_SHARE))
    ranked = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])
    chosen = set(ranked[:budget])
    return [i in chosen for i in range(len(words))]


def apply_fixes(words: list[CaptionWord], fixes: dict[str, str]) -> list[CaptionWord]:
    out = []
    for i, w in enumerate(words):
        fix = fixes.get(str(i))
        out.append(w.model_copy(update={"text": fix}) if fix and not w.censored else w)
    return out


def _ts(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_ts(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def display_windows(words: list[CaptionWord], duration: float) -> list[tuple[float, float]]:
    """Each word shows until the next starts; long pauses show nothing."""
    out = []
    for i, w in enumerate(words):
        nxt = words[i + 1].out_start if i + 1 < len(words) else duration
        end = nxt if nxt - w.out_end <= HIDE_AFTER_GAP else w.out_end + 0.2
        out.append((w.out_start, max(w.out_start + 0.08, min(end, duration))))
    return out


def ass_document(
    spec: EditSpec,
    *,
    font: str = "Arial Black",
    uppercase: bool = True,
    accent_bgr: str = "00D4FF",
) -> str:
    W, H = spec.width, spec.height
    fs = round(H * 0.072)
    outline = max(2, round(fs * 0.09))
    words = spec.captions
    windows = display_windows(words, spec.duration)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Word,{font},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,"
        f"0,0,1,{outline},{max(1, outline // 2)},5,0,0,0,1",
        f"Style: Emph,{font},{round(fs * 1.15)},&H00{accent_bgr},&H00{accent_bgr},&H00000000,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,{outline},{max(1, outline // 2)},5,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    x, y = W // 2, round(H * SAFE_Y)
    for w, (a, b) in zip(words, windows, strict=True):
        text = clean_word(w.text, uppercase)
        if not text:
            continue
        style = "Emph" if w.emphasis else "Word"
        size = fs * (1.15 if w.emphasis else 1.0)
        # Shrink a long word so it stays inside the safe width (≈0.62 em per character).
        est = len(text) * size * 0.62
        fs_tag = f"\\fs{round(size * MAX_TEXT_W * W / est)}" if est > MAX_TEXT_W * W else ""
        text = text.replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_ts(a)},{_ts(b)},{style},,0,0,0,,{{\\pos({x},{y}){fs_tag}}}{text}"
        )
    return "\n".join(lines) + "\n"


def srt_document(spec: EditSpec, uppercase: bool = False) -> str:
    out = []
    windows = display_windows(spec.captions, spec.duration)
    n = 0
    for w, (a, b) in zip(spec.captions, windows, strict=True):
        text = clean_word(w.text, uppercase)
        if not text:
            continue
        n += 1
        out += [str(n), f"{_srt_ts(a)} --> {_srt_ts(b)}", text, ""]
    return "\n".join(out)


def prepare_captions(
    spec: EditSpec, fixes: dict[str, str], hook: str, payoff: str
) -> tuple[list[CaptionWord], QAReport]:
    qa = QAReport(step="captions")
    words = apply_fixes(spec.captions, fixes)
    emph = pick_emphasis(words, hook, payoff)
    words = [w.model_copy(update={"emphasis": e}) for w, e in zip(words, emph, strict=True)]
    qa.check("captions present", bool(words), f"{len(words)} words, one at a time")
    qa.check(
        "captions timed inside the clip",
        all(0 <= w.out_start < w.out_end <= spec.duration + 1e-6 for w in words),
        f"{spec.duration:.2f} s",
    )
    censored = [w for w in words if w.censored]
    if censored:
        qa.check(
            "bleeped words censored in captions",
            all("*" in w.text for w in censored),
            ", ".join(w.text for w in censored),
        )
    if fixes:
        qa.check("owner caption fixes applied", True, f"{len(fixes)} word(s)", severity="warn")
    qa.check(
        "key-word emphasis",
        sum(emph) <= max(1, int(len(words) * MAX_EMPHASIS_SHARE)),
        f"{sum(emph)} emphasised",
        severity="warn",
    )
    return words, qa


def write_ass(spec: EditSpec, path: Path, **kw) -> Path:
    path.write_text(ass_document(spec, **kw), encoding="utf-8")
    return path
