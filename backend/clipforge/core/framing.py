"""Framing: a 9:16 camera path over the 16:9 source, and the host-reaction insert.

Camera (CLAUDE.md "Framing and host reaction"):
- follow the active speaker: the face whose track was matched to the diarized speaker talking
  now; else the face with the most mouth movement; else the biggest accepted face; else centre;
- rejected faces (small TV-wall/background faces) are never framed;
- smoothing: a deadzone (no move for small shifts) plus easing; a source shot change or a
  switch to another person is a clean hard cut, held at least MIN_HOLD seconds (no ping-pong);
- slow push-in: zoom grows PUSH_RATE per second from 1.0, capped, reset at every hard cut.

Host-reaction insert: if the host's face isn't on screen at any point in the clip, take
1.3-2 s of the host listening (host not speaking, mouth still, little frame difference) from
elsewhere in the episode, a different shot for every clip; video only, audio untouched.
"""

from __future__ import annotations

from typing import Any

from clipforge.core.faces import sample_index, speaker_at
from clipforge.core.render import base_crop_w
from clipforge.editspec import CameraKey, EditSpec, HostInsert
from clipforge.models import Transcript
from clipforge.qa import QAReport

KEY_STEP = 0.2  # seconds between camera keys
DEADZONE = 0.06  # share of crop width the subject may drift before the camera follows
EASE = 0.25  # fraction of the remaining offset covered per key
MIN_HOLD = 1.2  # seconds between hard cuts
PUSH_RATE = 0.004  # zoom per second (slow push-in)
PUSH_MAX = 1.10
HEADROOM = 0.12  # face centre sits this share of crop height above the frame centre
INSERT_MIN, INSERT_MAX = 1.3, 2.0
MOUTH_STILL = 0.35  # below this mouth difference the lips count as not moving


def _accepted(faces: list[dict], ident: dict) -> list[dict]:
    ts = ident.get("track_speaker", {})
    return [
        f
        for f in faces
        if not f.get("rejected") and not ts.get(str(f["track"]), {}).get("rejected")
    ]


def choose_subject(sample: dict, speaker: str | None, ident: dict) -> tuple[dict | None, str]:
    faces = _accepted(sample["faces"], ident)
    if not faces:
        return None, "no face: centre crop"
    ts = ident.get("track_speaker", {})
    if speaker:
        mine = [f for f in faces if ts.get(str(f["track"]), {}).get("speaker") == speaker]
        if mine:
            return max(mine, key=lambda f: f["box"][3]), f"active speaker {speaker}"
    moving = max(faces, key=lambda f: f["mouth"])
    if moving["mouth"] > MOUTH_STILL:
        return moving, "most mouth movement"
    return max(faces, key=lambda f: f["box"][3]), "largest face"


def plan_camera(
    spec: EditSpec,
    analysis: dict[str, Any] | None,
    ident: dict[str, Any] | None,
    transcript: Transcript,
    src_w: int,
    src_h: int,
) -> tuple[list[CameraKey], QAReport]:
    qa = QAReport(step="framing")
    crop_w = base_crop_w(src_w, src_h, spec.width, spec.height)
    crop_h = crop_w * spec.height / spec.width
    if not analysis or not analysis.get("samples"):
        qa.check(
            "face-tracked framing",
            False,
            "no face analysis for this episode: centre crop",
            severity="warn",
        )
        return [], qa
    ident = ident or {}
    keys: list[CameraKey] = []
    cx, cy = src_w / 2, src_h / 2
    zoom = 1.0
    last_cut_t = -1e9
    last_shot: int | None = None
    cur_track: int | None = None
    reasons: dict[str, int] = {}
    rejected_seen = 0
    person_cuts: list[float] = []

    def target(face: dict | None) -> tuple[float, float]:
        if face is None:
            return src_w / 2, src_h / 2
        x, y, w, h = face["box"]
        return x + w / 2, y + h / 2 + HEADROOM * crop_h

    t = 0.0
    while t <= spec.duration + 1e-9:
        src = spec.out_to_src(t)
        s = analysis["samples"][sample_index(analysis, src)]
        rejected_seen += sum(1 for f in s["faces"] if f.get("rejected"))
        face, why = choose_subject(s, speaker_at(transcript, src), ident)
        key = why.split(" ")[0]
        reasons[key] = reasons.get(key, 0) + 1
        cut = False
        if not keys:
            cx, cy = target(face)
            cur_track = face["track"] if face else None
        elif last_shot is not None and s["shot"] != last_shot:
            cx, cy = target(face)  # the source cut: cut with it
            cur_track = face["track"] if face else None
            cut, last_cut_t = True, t
        elif face is not None and face["track"] != cur_track:
            current = next(
                (f for f in _accepted(s["faces"], ident) if f["track"] == cur_track), None
            )
            if t - last_cut_t >= MIN_HOLD or current is None:
                cx, cy = target(face)  # clean cut to the new speaker
                cur_track = face["track"]
                cut, last_cut_t = True, t
                person_cuts.append(t)
            else:
                face = current  # hold on the current person until MIN_HOLD has passed
        if not cut and keys:
            tx, ty = target(face)
            if abs(tx - cx) > DEADZONE * crop_w:
                cx += (tx - cx) * EASE
            if abs(ty - cy) > DEADZONE * crop_h:
                cy += (ty - cy) * EASE
        zoom = 1.0 if cut or not keys else min(PUSH_MAX, zoom + PUSH_RATE * KEY_STEP)
        last_shot = s["shot"]
        keys.append(
            CameraKey(
                out_time=round(t, 4),
                cx=round(cx, 1),
                cy=round(cy, 1),
                crop_w=crop_w,
                zoom=round(zoom, 4),
                cut=cut,
            )
        )
        t += KEY_STEP
    cuts = sum(k.cut for k in keys)
    qa.check(
        "face-tracked framing",
        reasons.get("no", 0) < len(keys) * 0.5,
        ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) + f"; {cuts} reframe cut(s)",
        severity="warn",
    )
    gaps = [b - a for a, b in zip(person_cuts, person_cuts[1:], strict=False)]
    qa.check(
        "no camera ping-pong",
        all(g >= MIN_HOLD - 1e-6 for g in gaps),
        f"hard cuts at least {MIN_HOLD} s apart (except source shot changes)",
        severity="warn",
    )
    if rejected_seen:
        qa.check(
            "background faces ignored",
            True,
            f"{rejected_seen} small/background face detections never framed (TV wall etc.)",
            severity="warn",
        )
    return keys, qa


def host_on_screen(spec: EditSpec, analysis: dict, ident: dict) -> bool:
    host = ident.get("host_identity")
    if not host:
        return False
    idmap = ident.get("identities", {})
    t = 0.0
    while t <= spec.duration:
        s = analysis["samples"][sample_index(analysis, spec.out_to_src(t))]
        for f in _accepted(s["faces"], ident):
            if idmap.get(str(f["track"])) == host:
                return True
        t += 0.2
    return False


def find_host_insert(
    spec: EditSpec,
    analysis: dict[str, Any] | None,
    ident: dict[str, Any] | None,
    transcript: Transcript,
    host_speaker: str | None,
    used_shots: set[str],
    avoid: list[tuple[float, float]],
    src_w: int,
    src_h: int,
) -> tuple[HostInsert | None, QAReport]:
    qa = QAReport(step="host-insert")
    if not analysis or not ident or not host_speaker:
        qa.check(
            "host reaction insert",
            True,
            "not applicable (no face analysis or host)",
            severity="warn",
        )
        return None, qa
    if host_on_screen(spec, analysis, ident):
        qa.check(
            "host reaction insert", True, "host is on camera during the clip: no insert needed"
        )
        return None, qa
    host = ident.get("host_identity")
    if not host:
        qa.check(
            "host reaction insert",
            False,
            "host isn't on camera and their face couldn't be identified, so no insert",
            severity="warn",
        )
        return None, qa
    idmap = ident["identities"]
    samples = analysis["samples"]
    fps = analysis["sample_fps"]
    clip_lo = spec.pieces[0].src_start - 5
    clip_hi = spec.pieces[-1].src_end + 5
    # candidate samples: only the host's face on screen, host silent, lips still, steady frame
    good = []
    for s in samples:
        faces = _accepted(s["faces"], ident)
        ok = (
            len(faces) == 1
            and idmap.get(str(faces[0]["track"])) == host
            and faces[0]["mouth"] < MOUTH_STILL
            and s["diff"] < 6.0
            and speaker_at(transcript, s["t"]) != host_speaker
            and not (clip_lo <= s["t"] <= clip_hi)
        )
        good.append(ok)
    runs = []
    k = 0
    while k < len(good):
        if good[k]:
            j = k
            while j < len(good) and good[j] and samples[j]["shot"] == samples[k]["shot"]:
                j += 1
            dur = (j - k) / fps
            if dur >= INSERT_MIN:
                runs.append((k, j, dur))
            k = j
        else:
            k += 1
    shot_key = lambda k0: f"shot{samples[k0]['shot']}"  # noqa: E731
    runs = [r for r in runs if shot_key(r[0]) not in used_shots] or []
    if not runs:
        qa.check(
            "host reaction insert",
            False,
            "host isn't on camera and no unused listening shot of the host was found",
            severity="warn",
        )
        return None, qa
    k0, k1, dur = max(
        runs, key=lambda r: (r[2], -sum(samples[i]["diff"] for i in range(r[0], r[1])))
    )
    length = min(INSERT_MAX, dur - 0.2)
    src_start = samples[k0]["t"] + 0.1
    face = _accepted(samples[k0]["faces"], ident)[0]
    x, y, w, h = face["box"]
    crop_w = base_crop_w(src_w, src_h, spec.width, spec.height)
    crop_h = crop_w * spec.height / spec.width
    # place it while the guest talks, after the hook, clear of B-roll
    place = None
    t = 2.0
    while t + length <= spec.duration - 1.0:
        src = spec.out_to_src(t)
        if speaker_at(transcript, src) not in (None, host_speaker) and not any(
            a < t + length and t < b for a, b in avoid
        ):
            place = t
            break
        t += 0.2
    if place is None:
        qa.check(
            "host reaction insert",
            False,
            "no free spot for the insert in this clip",
            severity="warn",
        )
        return None, qa
    fps_out = spec.fps
    out_start = round(place * fps_out) / fps_out
    out_end = round((place + length) * fps_out) / fps_out
    insert = HostInsert(
        out_start=out_start,
        out_end=out_end,
        src_start=src_start,
        src_end=src_start + (out_end - out_start),
        cx=x + w / 2,
        cy=y + h / 2 + HEADROOM * crop_h,
        crop_w=crop_w,
        shot_id=shot_key(k0),
        reason="host off camera during the clip; listening shot, lips still",
    )
    qa.check(
        "host reaction insert",
        True,
        f"{out_end - out_start:.2f} s of the host listening at {out_start:.2f} s "
        f"(from {src_start:.2f} s, {insert.shot_id}); audio untouched",
        at_seconds=out_start,
    )
    return insert, qa
