"""Phase 3: framing, host-reaction insert, captions, mix, branded render."""

from __future__ import annotations

import numpy as np
import pytest

from clipforge.core import captions, faces, framing
from clipforge.core.audio import measure_loudness, write_wav
from clipforge.core.mix import mix
from clipforge.editspec import CaptionWord, EditSpec, Piece
from clipforge.models import Segment, Transcript, Word

SW, SH = 1920, 1080


def _spec(duration=20.0, start=100.0) -> EditSpec:
    return EditSpec(
        source_path="src.mp4",
        pieces=[Piece(src_start=start, src_end=start + duration)],
        width=1080,
        height=1920,
    )


def _transcript(turns: list[tuple[float, float, str]]) -> Transcript:
    segs = []
    for a, b, sp in turns:
        words = []
        t = a
        while t + 0.3 <= b:
            words.append(Word(text="word", start=t, end=t + 0.3, speaker=sp))
            t += 0.35
        segs.append(Segment(start=a, end=b, text="w", speaker=sp, words=words))
    return Transcript(duration=600, model="t", device="cpu", segments=segs)


def _analysis(fn, seconds=600.0) -> dict:
    """fn(t) -> (shot, faces)."""
    samples = []
    for k in range(int(seconds * 5)):
        t = k / 5
        shot, fs = fn(t)
        samples.append({"t": t, "shot": shot, "diff": 1.0, "faces": fs})
    return {"sample_fps": 5.0, "samples": samples, "tracks": {}, "shots": 1}


def _face(track, x, w=200, mouth=0.1, rejected=None, y=300):
    return {"track": track, "box": [x, y, w, w], "score": 0.9, "mouth": mouth, "rejected": rejected}


# ---- framing --------------------------------------------------------------------------------


def test_camera_follows_the_active_speaker_with_clean_cuts_and_hold():
    # Wide shot: host (track 0) on the left, guest (track 1) on the right.
    tr = _transcript([(100, 105, "S1"), (105, 105.5, "S2"), (105.5, 112, "S1"), (112, 120, "S2")])

    def at(t):
        return 0, [_face(0, 300), _face(1, 1400)]

    an = _analysis(at)
    ident = {"track_speaker": {"0": {"speaker": "S1"}, "1": {"speaker": "S2"}}}
    keys, qa = framing.plan_camera(_spec(), an, ident, tr, SW, SH)

    def cx_at(out_t):
        return min(keys, key=lambda k: abs(k.out_time - out_t)).cx

    assert cx_at(2) < 700  # host on the left
    assert cx_at(15) > 1200  # guest on the right
    # The 0.5 s guest interjection at 5 s doesn't make the camera jump away and back.
    cut_times = [k.out_time for k in keys if k.cut]
    gaps = [b - a for a, b in zip(cut_times, cut_times[1:], strict=False)]
    assert all(g >= framing.MIN_HOLD - 1e-6 for g in gaps)
    assert qa.passed


def test_tv_wall_faces_are_never_framed():
    tr = _transcript([(100, 120, "S2")])

    def at(t):
        # A tiny face on a TV wall (rejected) with lots of "mouth movement" + the real guest.
        return 0, [_face(5, 100, w=60, mouth=3.0, rejected="small face"), _face(1, 1300, w=260)]

    ident = {"track_speaker": {"1": {"speaker": "S2"}, "5": {"speaker": None}}}
    keys, qa = framing.plan_camera(_spec(), _analysis(at), ident, tr, SW, SH)
    assert all(k.cx > 1100 for k in keys)
    assert any(c.name == "background faces ignored" for c in qa.checks)


def test_push_in_is_slow_and_resets_on_cuts():
    tr = _transcript([(100, 120, "S1")])
    keys, _ = framing.plan_camera(
        _spec(), _analysis(lambda t: (0 if t < 110 else 1, [_face(0, 800)])), {}, tr, SW, SH
    )
    zooms = [k.zoom for k in keys]
    assert zooms[0] == 1.0 and max(zooms) <= framing.PUSH_MAX
    assert zooms[49] > zooms[0]  # creeping in
    cut = next(k for k in keys if k.cut)  # the source shot change at 10 s
    assert cut.zoom == 1.0 and abs(cut.out_time - 10.0) < 0.3


def test_no_faces_means_centre_crop_and_a_warning():
    keys, qa = framing.plan_camera(_spec(), None, None, _transcript([]), SW, SH)
    assert keys == [] and qa.checks[0].severity == "warn"


def test_host_insert_when_host_is_off_camera():
    # Clip (100-120 s) shows only the guest. Host listening shots exist at 300 s and 400 s.
    tr = _transcript([(100, 120, "S2"), (300, 304, "S2"), (400, 404, "S2"), (305, 310, "S1")])

    def at(t):
        if 300 <= t < 304:
            return 7, [_face(10, 800, mouth=0.05)]  # host solo, lips still
        if 400 <= t < 404:
            return 9, [_face(11, 800, mouth=0.05)]
        return 1, [_face(1, 1300)]

    an = _analysis(at)
    ident = {
        "track_speaker": {"1": {"speaker": "S2"}, "10": {"speaker": "S1"}, "11": {"speaker": "S1"}},
        "identities": {"1": "P2", "10": "P1", "11": "P1"},
        "host_identity": "P1",
    }
    spec = _spec()
    ins, qa = framing.find_host_insert(spec, an, ident, tr, "S1", set(), [], SW, SH)
    assert ins is not None and qa.passed
    assert framing.INSERT_MIN - 1e-6 <= ins.out_end - ins.out_start <= framing.INSERT_MAX + 1e-6
    assert ins.out_start >= 2.0  # after the hook
    assert ins.shot_id == "shot7"
    # A second clip must use a different listening shot.
    ins2, _ = framing.find_host_insert(spec, an, ident, tr, "S1", {"shot7"}, [], SW, SH)
    assert ins2 is not None and ins2.shot_id == "shot9"
    # And it stays clear of B-roll.
    ins3, _ = framing.find_host_insert(spec, an, ident, tr, "S1", set(), [(0, 15)], SW, SH)
    assert ins3 is not None and ins3.out_start >= 15


def test_no_insert_when_host_is_on_camera():
    tr = _transcript([(100, 120, "S2")])
    an = _analysis(lambda t: (0, [_face(0, 300), _face(1, 1300)]))
    ident = {"identities": {"0": "P1", "1": "P2"}, "host_identity": "P1", "track_speaker": {}}
    ins, qa = framing.find_host_insert(_spec(), an, ident, tr, "S1", set(), [], SW, SH)
    assert ins is None and "on camera" in qa.checks[0].detail


# ---- faces: tracks, shots, speaker matching ----------------------------------------------------


class ScriptedDetector:
    """Test double: returns faces we choose for each frame (by the frame's marker pixel)."""

    def __init__(self, script):
        self.script = script

    def detect(self, frame):
        return self.script(int(frame[0, 0, 0]))

    def embed(self, frame, face):
        return None


def test_analysis_tracks_mouths_and_matches_speakers():
    rng = np.random.default_rng(0)
    frames = []
    for k in range(100):  # 20 s at 5 fps
        f = np.full((360, 640, 3), 80, np.uint8)
        f[0, 0, 0] = k
        # Speaker A (left) talks in the first 10 s, B (right) in the last 10 s: draw moving mouths.
        talking_left = k < 50
        for cx, talking in ((160, talking_left), (480, not talking_left)):
            f[200:230, cx - 30 : cx + 30] = rng.integers(0, 255, (30, 60, 3)) if talking else 120
        frames.append((k / 5, f))

    def script(k):
        return [
            faces.Face(box=(110, 120, 100, 120), score=0.9),
            faces.Face(box=(430, 120, 100, 120), score=0.9),
        ]

    an = faces.analyze_samples(frames, ScriptedDetector(script))
    assert len(an["tracks"]) == 2
    tr = _transcript([(0, 10, "A"), (10, 20, "B")])
    ident = faces.identify(an, tr, host_speaker="A")
    by_track = {tid: v["speaker"] for tid, v in ident["track_speaker"].items()}
    left = next(t for t, info in an["tracks"].items() if info["n"] and t == "0")
    assert by_track[left] == "A" and set(by_track.values()) == {"A", "B"}
    assert ident["host_identity"] is not None


def test_shot_change_and_small_faces():
    frames = [(k / 5, np.full((360, 640, 3), 30 if k < 10 else 220, np.uint8)) for k in range(20)]
    for k, (_, f) in enumerate(frames):
        f[0, 0, 0] = k

    def script(k):
        return [
            faces.Face(box=(100, 100, 200, 200), score=0.9),
            faces.Face(box=(500, 20, 40, 40), score=0.8),
        ]

    an = faces.analyze_samples(frames, ScriptedDetector(script))
    assert an["shots"] == 2
    small = [f for s in an["samples"] for f in s["faces"] if f["rejected"]]
    assert small and all(f["box"][2] == 40 for f in small)


def test_yunet_runs_and_finds_no_face_in_noise():
    det = faces.YuNetDetector()
    frame = np.random.default_rng(1).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    assert det.detect(frame) == []


# ---- captions -------------------------------------------------------------------------------


def _words(texts, censored=()):
    out = []
    t = 0.2
    for i, w in enumerate(texts):
        out.append(
            CaptionWord(
                text=captions_censor(w) if i in censored else w,
                spoken=w,
                out_start=round(t, 3),
                out_end=round(t + 0.3, 3),
                src_start=t,
                src_end=t + 0.3,
                censored=i in censored,
            )
        )
        t += 0.35
    return out


def captions_censor(w):
    from clipforge.core.cut import censor

    return censor(w)


def test_captions_one_word_per_event_safe_zone_and_emphasis():
    spec = _spec(5.0)
    words = _words("I turned down two million dollars from Marcus Hale tonight.".split())
    spec.captions = words
    spec.captions, qa = captions.prepare_captions(spec, {}, "turned down two million dollars", "")
    doc = captions.ass_document(spec, uppercase=True)
    events = [ln for ln in doc.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == len(words)  # one word at a time
    assert all("\\pos(540,1190)" in e for e in events)  # centred, inside the safe zone
    emph = [e.split(",")[3] for e in events]
    assert emph.count("Emph") <= max(1, int(len(words) * 0.25))
    assert any("MILLION" in e and ",Emph," in e for e in events) or any(
        "MARCUS" in e and ",Emph," in e for e in events
    )
    assert "TONIGHT" in doc and "TONIGHT." not in doc  # punctuation stripped
    assert qa.passed


def test_captions_fixes_censoring_and_srt():
    spec = _spec(5.0)
    spec.captions = _words(["what", "the", "fuck", "happend"], censored={2})
    spec.captions, _ = captions.prepare_captions(spec, {"3": "happened", "2": "nope"}, "", "")
    texts = [w.text for w in spec.captions]
    assert texts[3] == "happened"  # owner's fix
    assert texts[2] == "f**k"  # censored words can't be "fixed" back
    srt = captions.srt_document(spec)
    assert "f**k" in srt and "-->" in srt


def test_long_word_is_shrunk_to_fit():
    spec = _spec(3.0)
    spec.captions = _words(["supercalifragilisticexpialidocious"])
    assert "\\fs" in captions.ass_document(spec)


# ---- mix ------------------------------------------------------------------------------------


@pytest.fixture
def stems(tmp_path):
    from clipforge.testing import synth

    voice, _ = synth.make_audio(15)
    write_wav(tmp_path / "voice.wav", voice * 0.2)
    t = np.arange(48000 * 7) / 48000
    music = 0.3 * (np.sin(2 * np.pi * 220 * t) + 0.5 * np.sin(2 * np.pi * 330 * t))
    write_wav(tmp_path / "music.wav", music.astype(np.float32))
    return tmp_path


def test_mix_hits_targets_with_music(stems):
    qa = mix(
        stems / "voice.wav",
        stems / "mix.wav",
        bgm=stems / "music.wav",
        duration=15.0,
        workdir=stems / "w",
    )
    assert qa.passed, qa.failures
    detail = {c.name: c.detail for c in qa.checks}
    assert "-14.0 LUFS" in detail["voice at about -14 LUFS"]
    under = float(
        detail["music about 19 dB under the voice (ducked)"].split("= ")[1].split(" dB")[0]
    )
    assert 17.5 <= under <= 20.5
    assert measure_loudness(stems / "mix.wav")["true_peak"] <= -0.7


def test_mix_without_music_warns_but_passes(stems):
    qa = mix(stems / "voice.wav", stems / "m.wav", bgm=None, duration=15.0, workdir=stems / "w2")
    assert qa.passed
    assert any(c.name == "background music" and c.severity == "warn" for c in qa.checks)


def test_limiter_holds_minus_one_dbtp_on_hot_voice(tmp_path):
    t = np.arange(48000 * 6) / 48000
    hot = (0.98 * np.sign(np.sin(2 * np.pi * 180 * t)) * (np.sin(2 * np.pi * 2 * t) > 0)).astype(
        np.float32
    )
    write_wav(tmp_path / "hot.wav", hot)
    qa = mix(tmp_path / "hot.wav", tmp_path / "o.wav", duration=6.0, workdir=tmp_path / "w")
    tp = measure_loudness(tmp_path / "o.wav")["true_peak"]
    assert tp <= -0.7, tp
    assert next(c for c in qa.checks if c.name.startswith("limiter")).passed
