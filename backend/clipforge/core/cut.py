"""Cut-point engine: zero clipped words.

Rules (CLAUDE.md, "Zero-clipped-words rules"):
- Never cut at a whisper timestamp. Whisper only says roughly where a gap between two words
  is; the cut goes at the minimum of a 10 ms RMS trace inside a measured silent run.
- Noisy audio is judged by dips relative to the local floor (see `Levels.threshold_db`).
- No real gap near the requested point -> extend to the next natural pause.
- Every cut is on the frame grid and keeps >= GUARD seconds of silence on both sides, so the
  verification gate's "RMS 40-60 ms before and after every join" check passes by design.
- Pauses are shortened only inside strict silence, keeping a margin on both sides.
- Profanity: prefer a different seam (skip the sentence) when that leaves a complete clip;
  otherwise bleep exactly the word; never damage neighbouring speech.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from clipforge.core.audio import HOP, Levels, grid_points, local_levels, rms_trace, silent_runs
from clipforge.core.pick import _is_profane
from clipforge.editspec import Bleep, CaptionWord, Cut, EditSpec, Join, Piece
from clipforge.models import Transcript, Word
from clipforge.qa import QAReport

HARD_MAX_SECONDS = 30.0


class CutError(Exception):
    def __init__(self, message: str, at_seconds: float | None = None) -> None:
        super().__init__(message)
        self.at_seconds = at_seconds


@dataclass(frozen=True)
class CutConfig:
    fps: int = 60
    min_gap: float = 0.14  # shortest silent run accepted for a cut
    guard: float = 0.06  # silence kept on each side of every cut
    search_pad: float = 0.55  # allowance for whisper timing error left after drift correction
    extend_max: float = 6.0  # how far to look for the next natural pause
    lead_in: float = 0.20  # most silence allowed before the first word (hook lands fast)
    tail_out: float = 0.40  # most silence after the last word (natural decay, < 0.45 s)
    max_pause: float = 0.45  # longer pauses inside a clip get shortened
    pause_keep: float = 0.12  # silence kept on each side of a shortened pause
    pause_strict_db: float = 3.0  # a removable pause must be this far below the threshold
    min_clip: float = 18.0
    max_clip: float = HARD_MAX_SECONDS
    profanity_mode: str = "prefer_alternative_then_bleep"  # | "bleep" | "allow"


@dataclass
class _Window:
    first: int  # word indices (inclusive)
    last: int
    start: Cut
    end: Cut


@dataclass
class CutResult:
    spec: EditSpec
    qa: QAReport
    flags: list[str] = field(default_factory=list)


def _sentence_spans(words: list[Word], first: int, last: int) -> list[tuple[int, int]]:
    spans, s = [], first
    for i in range(first, last + 1):
        if re.search(r"[.?!][\"')\]]*$", words[i].text) or i == last:
            spans.append((s, i))
            s = i + 1
    return spans


def censor(word: str) -> str:
    """f**k-style caption censoring that keeps surrounding punctuation."""
    m = re.match(r"^(\W*)(\w+)(\W*)$", word)
    if not m:
        return "*" * len(word)
    pre, core, post = m.groups()
    if len(core) <= 2:
        return pre + core[0] + "*" * (len(core) - 1) + post
    return pre + core[0] + "*" * (len(core) - 2) + core[-1] + post


class CutEngine:
    def __init__(self, audio48: np.ndarray, transcript: Transcript, cfg: CutConfig) -> None:
        self.audio = audio48
        self.sr = 48000
        self.cfg = cfg
        self.trace = rms_trace(audio48, self.sr)
        self.duration = len(audio48) / self.sr
        self.words = sorted(transcript.words, key=lambda w: w.start)
        self._lag_cache: dict[int, float] = {}

    # ---- primitives ---------------------------------------------------------------------------

    def _frame(self, t: float) -> int:
        return max(0, min(len(self.trace) - 1, int(t / HOP)))

    def levels_at(self, t: float) -> Levels:
        return local_levels(self.trace, self._frame(t))

    def lag_at(self, t: float) -> float:
        """Whisper's local timing drift near `t` (seconds; positive = whisper is late).

        Found by cross-correlating whisper's word layout with measured speech energy over a
        ±5 s window, lags ±0.5 s. Cached per second."""
        key = int(t)
        if key in self._lag_cache:
            return self._lag_cache[key]
        lo, hi = max(0.0, key - 5.0), min(self.duration, key + 6.0)
        f0, f1 = int(lo / HOP), int(hi / HOP)
        if f1 - f0 < 100:
            return 0.0
        levels = self.levels_at(t)
        energy = (self.trace[f0:f1] >= levels.threshold_db).astype(np.float32)
        layout = np.zeros(f1 - f0, dtype=np.float32)
        for w in self.words:
            if w.end < lo - 1 or w.start > hi + 1:
                continue
            a = max(0, int(w.start / HOP) - f0)
            b = min(f1 - f0, int(w.end / HOP) - f0)
            if b > a:
                layout[a:b] = 1.0
        if layout.sum() == 0 or energy.sum() == 0:
            self._lag_cache[key] = 0.0
            return 0.0
        # Smooth both over ~150 ms so pauses, not the word-by-word rhythm, drive the match
        # (word rhythm repeats every ~0.3 s and would give several equally good lags).
        k = np.ones(15, dtype=np.float32) / 15
        energy = np.convolve(energy, k, mode="same")
        layout = np.convolve(layout, k, mode="same")
        e = energy - energy.mean()
        lay = layout - layout.mean()
        best, best_lag = -np.inf, 0
        for lag in range(-50, 51):  # ±0.5 s in 10 ms steps
            if lag >= 0:
                score = float(np.dot(lay[lag:], e[: len(e) - lag]))
            else:
                score = float(np.dot(lay[:lag], e[-lag:]))
            if score > best:
                best, best_lag = score, lag
        self._lag_cache[key] = best_lag * HOP
        return self._lag_cache[key]

    def span(self, i: int) -> tuple[float, float]:
        """Word i's whisper times, corrected for local drift."""
        w = self.words[i]
        lag = self.lag_at((w.start + w.end) / 2)
        return w.start - lag, w.end - lag

    def _mid(self, i: int) -> float:
        a, b = self.span(i)
        return (a + b) / 2

    def _gap(self, k: int) -> tuple[float, float]:
        """Drift-corrected whisper gap before word k (k == len(words): after the last)."""
        lo = self.span(k - 1)[1] if k > 0 else 0.0
        hi = self.span(k)[0] if k < len(self.words) else self.duration
        return lo, hi

    def _place(self, run: tuple[int, int], edge: str, levels: Levels) -> tuple[float, float] | None:
        """Cut time inside a silent run, on the frame grid, with GUARD silence both sides."""
        c = self.cfg
        rs, re_ = run[0] * HOP, run[1] * HOP
        lo, hi = rs + c.guard, re_ - c.guard
        if edge == "start":  # speech starts at re_: keep the lead-in short
            lo = max(lo, re_ - c.lead_in)
        elif edge == "end":  # speech ended at rs: let it decay, then cut
            hi = min(hi, rs + c.tail_out)
        if hi < lo:
            return None
        f_lo, f_hi = int(math.ceil(lo / HOP)), int(math.floor(hi / HOP))
        if f_hi < f_lo:
            f_lo = f_hi = self._frame((lo + hi) / 2)
        seg = self.trace[f_lo : f_hi + 1]
        target = (f_lo + int(np.argmin(seg)) + 0.5) * HOP
        points = grid_points(lo, hi, self.cfg.fps)
        if not points:
            return None
        t = min(points, key=lambda p: abs(p - target))
        return t, float(seg.min())

    def cut_between(self, i: int, edge: str, reason: str, requested: float) -> Cut | None:
        """A cut in the gap between word i-1 and word i (i == len(words) means after the last
        word), or None if there is no real silence there."""
        c = self.cfg
        gap_lo, gap_hi = self._gap(i)
        # Whisper's gap can still be off after drift correction: search around it. Only
        # measured silence qualifies, so a cut can never land inside a word.
        lo = max(0.0, min(gap_lo, gap_hi) - c.search_pad)
        hi = min(self.duration, max(gap_lo, gap_hi) + c.search_pad)
        if hi - lo < c.min_gap:
            return None
        levels = self.levels_at((gap_lo + gap_hi) / 2)
        thr = levels.threshold_db
        runs = silent_runs(
            self.trace,
            thr,
            int(lo / HOP),
            int(math.ceil(hi / HOP)),
            int(math.ceil(c.min_gap / HOP)),
        )
        if not runs:
            return None
        # Measure each run's full extent (the search window may have clipped it), so the
        # lead-in/tail limits use the real speech onset/offset.
        full = []
        for r0, r1 in runs:
            while r0 > 0 and self.trace[r0 - 1] < thr:
                r0 -= 1
            while r1 < len(self.trace) and self.trace[r1] < thr:
                r1 += 1
            full.append((r0, r1))
        # Order guard: a start cut must not come after its first word, nor an end cut before
        # its last word (judged by drift-corrected whisper times). If unsure, the caller
        # extends to the next pause: the clip may gain a word, never silently lose one.
        if edge == "start" and i < len(self.words):
            ws, we = self.span(i)
            full = [r for r in full if r[0] * HOP < (ws + we) / 2]
        if edge == "end" and i > 0:
            ws, we = self.span(i - 1)
            full = [r for r in full if r[1] * HOP > (ws + we) / 2]
        runs = sorted(set(full))
        if not runs:
            return None

        def overlap(run: tuple[int, int], g: tuple[float, float]) -> float:
            return max(0.0, min(run[1] * HOP, g[1]) - max(run[0] * HOP, g[0]))

        def dist(run: tuple[int, int], g: tuple[float, float]) -> float:
            return abs((run[0] + run[1]) * HOP / 2 - (g[0] + g[1]) / 2)

        def score(run: tuple[int, int]) -> tuple[float, float]:
            return (-overlap(run, (gap_lo, gap_hi)), dist(run, (gap_lo, gap_hi)))

        # Which words end up in the clip is checked against the audio afterwards (the gate
        # re-transcribes it and compares with the intended words), so a timing ambiguity
        # can't pass unnoticed.
        for run in sorted(runs, key=score):
            placed = self._place(run, edge, levels)
            if placed:
                t, min_db = placed
                return Cut(
                    edge=edge,  # type: ignore[arg-type]
                    src_time=t,  # exact k/fps: rounding would move it off the grid
                    requested=round(requested, 3),
                    reason=reason,  # type: ignore[arg-type]
                    silence_from=round(run[0] * HOP, 3),
                    silence_to=round(run[1] * HOP, 3),
                    min_rms_db=round(min_db, 1),
                    threshold_db=round(thr, 1),
                )
        return None

    def resolve(self, i: int, edge: str, reason: str, requested: float) -> tuple[Cut, int]:
        """Cut near gap i; if there's no real gap, move outward to the next natural pause.

        A start edge moves earlier (the clip gains words at the front); an end edge moves
        later (the clip gains words at the back). Returns the cut and the new gap index."""
        c = self.cfg
        cut = self.cut_between(i, edge, reason, requested)
        if cut:
            return cut, i
        step = -1 if edge == "start" else 1
        k = i + step
        while 0 <= k <= len(self.words):
            anchor = self.span(i)[0] if edge == "start" else self.span(i - 1)[1]
            here = self.span(k)[0] if edge == "start" else self.span(k - 1)[1]
            if abs(here - anchor) > c.extend_max:
                break
            cut = self.cut_between(k, edge, reason, requested)
            if cut:
                cut.extended_by = round(abs(here - anchor), 3)
                return cut, k
            k += step
        raise CutError(
            f"No silence to cut in within {c.extend_max:.0f} s of {requested:.2f} s "
            + ("(before the first word)" if edge == "start" else "(after the last word)")
            + "; the speech runs on without a pause here.",
            at_seconds=requested,
        )

    def word_range(self, start: float, end: float) -> tuple[int, int]:
        """Words a requested window covers. Requests come from the picker in whisper's own
        time, so compare against raw whisper times here (not drift-corrected ones)."""
        idx = [i for i, w in enumerate(self.words) if start <= (w.start + w.end) / 2 <= end]
        if not idx:
            raise CutError(f"No words between {start:.2f} and {end:.2f} s", at_seconds=start)
        return idx[0], idx[-1]

    def snap_to_silence(self, t: float, edge: str) -> tuple[Cut, int]:
        """Nearest clean cut, in either direction, for a trim handle dragged to `t`.
        Returns the cut and the gap index (words before it are out for a start edge, in for
        an end edge)."""
        n = len(self.words)
        i = next((k for k in range(n) if self._mid(k) >= t), n)
        best: tuple[Cut, int] | None = None
        for step in (-1, 1):
            k = i if step == 1 else i - 1
            while 0 <= k <= n:
                here = self._gap(k)
                if abs((here[0] + here[1]) / 2 - t) > self.cfg.extend_max:
                    break
                cut = self.cut_between(k, edge, "trim", t)
                if cut:
                    if best is None or abs(cut.src_time - t) < abs(best[0].src_time - t):
                        best = (cut, k)
                    break
                k += step
        if best is None:
            raise CutError(
                f"No silence within {self.cfg.extend_max:.0f} s of {t:.2f} s to trim to",
                at_seconds=t,
            )
        return best

    # ---- profanity --------------------------------------------------------------------------

    def bleep_span(self, i: int) -> Bleep:
        """The word's extent from the RMS trace: from the dip before it to the dip after it,
        never past the middle of a neighbouring word."""
        w = self.words[i]
        ws, we = self.span(i)
        lo = max(ws - 0.15, self._mid(i - 1) if i > 0 else 0.0)
        hi = min(we + 0.15, self._mid(i + 1) if i + 1 < len(self.words) else self.duration)
        a0, a1 = self._frame(lo), self._frame(min(ws + 0.05, we))
        b0, b1 = self._frame(max(we - 0.05, ws)), self._frame(hi)
        s = (a0 + int(np.argmin(self.trace[a0 : a1 + 1]))) * HOP if a1 >= a0 else ws
        e = (b0 + int(np.argmin(self.trace[b0 : b1 + 1])) + 1) * HOP if b1 >= b0 else we
        return Bleep(src_start=round(s, 3), src_end=round(max(e, s + 0.05), 3), word=w.text)

    # ---- main entry -----------------------------------------------------------------------------

    def build(
        self,
        segments: list[tuple[float, float]],
        source_path: str,
        source_fps: float | None = None,
        trims: dict[int, tuple[float | None, float | None]] | None = None,
    ) -> CutResult:
        c = self.cfg
        qa = QAReport(step="cut")
        flags: list[str] = []
        trims = trims or {}

        # 1. Each requested window -> a word range with silence-based edges.
        windows: list[_Window] = []
        ordered = sorted(segments)
        intended: list[str] = []
        for n, (s0, s1) in enumerate(ordered):
            t_start, t_end = trims.get(n, (None, None))
            first, last = self.word_range(s0, s1)
            if t_start is None and t_end is None:
                intended += [self.words[k].text for k in range(first, last + 1)]
            # Word membership follows the gaps the cuts were placed in, not whisper's times.
            if t_start is not None:
                start_cut, first = self.snap_to_silence(t_start, "start")
            else:
                reason = "clip_start" if n == 0 else "seam"
                start_cut, first = self.resolve(first, "start", reason, s0)
            if t_end is not None:
                end_cut, gap = self.snap_to_silence(t_end, "end")
            else:
                reason = "clip_end" if n == len(ordered) - 1 else "seam"
                end_cut, gap = self.resolve(last + 1, "end", reason, s1)
            last = gap - 1
            if end_cut.src_time <= start_cut.src_time or last < first:
                raise CutError("The clip's end is before its start", at_seconds=s0)
            if windows and start_cut.src_time < windows[-1].end.src_time:
                raise CutError("Stitched windows overlap after snapping to silence", s0)
            if t_start is not None or t_end is not None:
                intended += [self.words[k].text for k in range(first, last + 1)]
            windows.append(_Window(first, last, start_cut, end_cut))
            for cut in (start_cut, end_cut):
                if cut.extended_by:
                    flags.append(
                        f"{cut.edge} at {cut.requested:.2f} s moved {cut.extended_by:.2f} s to "
                        "the next natural pause (no real gap there)"
                    )

        # 2. Profanity.
        bleeps: list[Bleep] = []
        removed: list[dict] = []
        profane = [
            i
            for w in windows
            for i in range(w.first, w.last + 1)
            if _is_profane(self.words[i].text)
        ]
        if profane and c.profanity_mode == "allow":
            flags.append(f"{len(profane)} profane word(s) left in (profanity mode: allow)")
        elif profane:
            all_spans = [s for w in windows for s in _sentence_spans(self.words, w.first, w.last)]
            hook, payoff = all_spans[0], all_spans[-1]
            for i in profane:
                if any(r["first"] <= i <= r["last"] for r in removed):
                    continue
                span = next(s for s in all_spans if s[0] <= i <= s[1])
                skipped = False
                if c.profanity_mode == "prefer_alternative_then_bleep" and span not in (
                    hook,
                    payoff,
                ):
                    skipped = self._try_skip(windows, span, removed)
                if not skipped:
                    bleeps.append(self.bleep_span(i))

        # 3. Pieces, with long pauses shortened inside strict silence.
        pieces: list[Piece] = []
        piece_words: list[list[int]] = []
        cuts: list[Cut] = []
        join_reasons: list[str] = []
        for w in windows:
            sub = self._shorten_pauses(w.start, w.end)
            first_piece = len(pieces)
            for a, b in sub:
                if pieces:
                    join_reasons.append(a.reason)
                pieces.append(Piece(src_start=a.src_time, src_end=b.src_time))
                piece_words.append([])
                cuts += [a, b]
            # Assign this window's words to its pieces in order (nearest piece by whisper
            # time, never going backwards), so a timing error can't drop a word.
            k = first_piece
            for i in range(w.first, w.last + 1):
                mid = self._mid(i)
                while (
                    k + 1 < len(pieces) and mid >= (pieces[k].src_end + pieces[k + 1].src_start) / 2
                ):
                    k += 1
                piece_words[k].append(i)

        removed_words = {k for r in removed for k in range(r["first"], r["last"] + 1)}
        removed_text = [self.words[k].text for k in sorted(removed_words)]
        for wtxt in removed_text:
            if wtxt in intended:
                intended.remove(wtxt)
        bleeped_words = {b.word for b in bleeps}
        spec = EditSpec(
            source_path=source_path,
            source_fps=source_fps,
            fps=c.fps,
            pieces=pieces,
            cuts=cuts,
            bleeps=bleeps,
            removed=removed,
            intended_words=[w for w in intended if w not in bleeped_words],
        )
        offs = spec.out_offsets()
        spec.joins = [
            Join(
                out_time=round(offs[k], 6),
                src_before=pieces[k - 1].src_end,
                src_after=pieces[k].src_start,
                reason=join_reasons[k - 1],
            )
            for k in range(1, len(pieces))
        ]
        spec.captions = self.captions(spec, piece_words)

        # 4. QA.
        for cut in cuts:
            qa.check(
                f"{cut.reason} {cut.edge} cut in silence",
                cut.min_rms_db < cut.threshold_db,
                f"at {cut.src_time:.3f} s: {cut.min_rms_db} dB in a "
                f"{cut.silence_to - cut.silence_from:.2f} s silent run "
                f"(threshold {cut.threshold_db} dB)",
                at_seconds=cut.src_time,
            )
        qa.check(
            "every cut on the frame grid",
            all(abs(cut.src_time * c.fps - round(cut.src_time * c.fps)) < 1e-6 for cut in cuts),
            f"{c.fps} fps",
        )
        dur = spec.duration
        qa.check("clip within the 30 s hard max", dur <= HARD_MAX_SECONDS + 1e-9, f"{dur:.2f} s")
        qa.check(
            f"clip length {c.min_clip:.0f}-{c.max_clip:.0f} s",
            c.min_clip <= dur <= c.max_clip,
            f"{dur:.2f} s",
            severity="warn",
        )
        for f in flags:
            qa.check("cut moved to a natural pause", True, f, severity="warn")
        for r in removed:
            qa.check(
                "profanity avoided with a different seam",
                True,
                f'skipped "{r["text"]}"',
                severity="warn",
                at_seconds=r["start"],
            )
        for b in bleeps:
            qa.check(
                "profanity bleeped (no clean alternative seam)",
                True,
                f'"{b.word}" {b.src_start:.2f}-{b.src_end:.2f} s',
                severity="warn",
                at_seconds=b.src_start,
            )
        if dur > HARD_MAX_SECONDS + 1e-9:
            raise CutError(
                f"Clip is {dur:.2f} s after cutting in silence, over the 30 s hard max. "
                "Trim it in the editor.",
                at_seconds=pieces[0].src_start,
            )
        return CutResult(spec=spec, qa=qa, flags=flags)

    def _try_skip(self, windows: list[_Window], span: tuple[int, int], removed: list[dict]) -> bool:
        """Cut a sentence out with two silence cuts if the clip stays long enough."""
        a, b = span
        w_idx = next((k for k, w in enumerate(windows) if w.first <= a and b <= w.last), None)
        if w_idx is None or a == windows[w_idx].first or b == windows[w_idx].last:
            return False
        end_cut = self.cut_between(a, "end", "profanity_skip", self.words[a].start)
        start_cut = self.cut_between(b + 1, "start", "profanity_skip", self.words[b].end)
        if not end_cut or not start_cut or start_cut.src_time <= end_cut.src_time:
            return False
        total = sum(w.end.src_time - w.start.src_time for w in windows)
        if total - (start_cut.src_time - end_cut.src_time) < self.cfg.min_clip:
            return False
        w = windows[w_idx]
        windows[w_idx : w_idx + 1] = [
            _Window(w.first, a - 1, w.start, end_cut),
            _Window(b + 1, w.last, start_cut, w.end),
        ]
        removed.append(
            {
                "first": a,
                "last": b,
                "start": self.words[a].start,
                "end": self.words[b].end,
                "text": " ".join(x.text for x in self.words[a : b + 1]),
                "reason": "profanity",
            }
        )
        return True

    def _shorten_pauses(self, start: Cut, end: Cut) -> list[tuple[Cut, Cut]]:
        c = self.cfg
        a, b = start.src_time, end.src_time
        out: list[tuple[Cut, Cut]] = []
        cur = start
        levels = self.levels_at((a + b) / 2)
        thr = levels.threshold_db - c.pause_strict_db
        runs = silent_runs(
            self.trace,
            thr,
            int((a + c.guard) / HOP) + 1,
            int((b - c.guard) / HOP),
            int(math.ceil(c.max_pause / HOP)),
        )
        for r0, r1 in runs:
            rs, re_ = r0 * HOP, r1 * HOP
            e_pts = grid_points(rs + c.guard, rs + c.pause_keep, c.fps)
            s_pts = grid_points(re_ - c.pause_keep, re_ - c.guard, c.fps)
            if not e_pts or not s_pts or s_pts[0] - e_pts[-1] < 0.1:
                continue
            seg = self.trace[r0:r1]
            common = dict(
                silence_from=round(rs, 3),
                silence_to=round(re_, 3),
                min_rms_db=round(float(seg.min()), 1),
                threshold_db=round(thr, 1),
                reason="pause",
                requested=round(rs, 3),
            )
            e = Cut(edge="end", src_time=e_pts[-1], **common)
            s = Cut(edge="start", src_time=s_pts[0], **common)
            out.append((cur, e))
            cur = s
        out.append((cur, end))
        return out

    def captions(self, spec: EditSpec, piece_words: list[list[int]]) -> list[CaptionWord]:
        bleeped = [(b.src_start, b.src_end) for b in spec.bleeps]
        out: list[CaptionWord] = []
        for off, p, idx in zip(spec.out_offsets(), spec.pieces, piece_words, strict=True):
            for i in idx:
                w = self.words[i]
                mid = (w.start + w.end) / 2
                is_bleeped = any(s - 0.15 <= mid <= e + 0.15 for s, e in bleeped) and _is_profane(
                    w.text
                )
                # Clamp whisper's times into the piece the word belongs to.
                s = min(max(w.start, p.src_start), p.src_end)
                e = min(max(w.end, s + 0.05), p.src_end)
                out.append(
                    CaptionWord(
                        text=censor(w.text) if is_bleeped else w.text,
                        spoken=w.text,
                        out_start=round(off + s - p.src_start, 3),
                        out_end=round(off + e - p.src_start, 3),
                        src_start=w.start,
                        src_end=w.end,
                        censored=is_bleeped,
                        speaker=w.speaker,
                    )
                )
        return out
