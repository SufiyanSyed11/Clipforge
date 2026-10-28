"use client";

import { useEffect, useRef, useState } from "react";
import { api, fmtTime } from "@/lib/api";

type Wave = { start: number; hop: number; db: number[]; pieces: { src_start: number; src_end: number }[] };

/** Episode waveform around the clip with draggable start/end handles for each segment.
 *  Handles snap to real silence when the clip is rebuilt (the cut engine does that). */
export default function Waveform({
  clipId,
  segments,
  onTrim,
}: {
  clipId: number;
  segments: { start: number; end: number }[];
  onTrim: (segment: number, edge: "start" | "end", t: number) => void;
}) {
  const [wave, setWave] = useState<Wave | null>(null);
  const [drag, setDrag] = useState<{ seg: number; edge: "start" | "end"; t: number } | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    api<Wave>(`/api/clips/${clipId}/waveform`).then(setWave).catch(() => setWave(null));
  }, [clipId, segments]);

  const W = 900;
  const H = 120;
  const span = wave ? wave.db.length * wave.hop : 1;
  const x = (t: number) => (wave ? ((t - wave.start) / span) * W : 0);
  const t = (px: number) => (wave ? wave.start + (px / W) * span : 0);

  useEffect(() => {
    const c = canvas.current;
    if (!c || !wave) return;
    const g = c.getContext("2d")!;
    g.clearRect(0, 0, W, H);
    g.fillStyle = "#12151a";
    g.fillRect(0, 0, W, H);
    for (const p of wave.pieces) {
      g.fillStyle = "rgba(249,115,22,0.15)";
      g.fillRect(x(p.src_start), 0, x(p.src_end) - x(p.src_start), H);
    }
    g.fillStyle = "#9ca3af";
    const bw = W / wave.db.length;
    wave.db.forEach((d, i) => {
      const v = Math.max(0, (d + 70) / 70);
      const h = v * (H - 10);
      g.fillRect(i * bw, (H - h) / 2, Math.max(1, bw - 0.5), h);
    });
    segments.forEach((s, i) => {
      for (const edge of ["start", "end"] as const) {
        const tt = drag && drag.seg === i && drag.edge === edge ? drag.t : s[edge];
        g.fillStyle = edge === "start" ? "#22c55e" : "#ef4444";
        g.fillRect(x(tt) - 2, 0, 4, H);
      }
    });
  }, [wave, segments, drag]);

  function hit(px: number) {
    let best: { seg: number; edge: "start" | "end"; d: number } | null = null;
    segments.forEach((s, i) =>
      (["start", "end"] as const).forEach((edge) => {
        const d = Math.abs(x(s[edge]) - px);
        if (d < 12 && (!best || d < best.d)) best = { seg: i, edge, d };
      }),
    );
    return best as { seg: number; edge: "start" | "end"; d: number } | null;
  }

  function px(e: React.PointerEvent<HTMLCanvasElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    return ((e.clientX - r.left) / r.width) * W;
  }

  if (!wave) return <p className="text-sm text-gray-500">Waveform appears after the first build.</p>;
  return (
    <div className="space-y-1">
      <canvas
        ref={canvas}
        width={W}
        height={H}
        data-testid="waveform"
        className="w-full cursor-ew-resize rounded"
        onPointerDown={(e) => {
          const h = hit(px(e));
          if (h) {
            e.currentTarget.setPointerCapture(e.pointerId);
            setDrag({ seg: h.seg, edge: h.edge, t: t(px(e)) });
          }
        }}
        onPointerMove={(e) => drag && setDrag({ ...drag, t: t(px(e)) })}
        onPointerUp={() => {
          if (drag) onTrim(drag.seg, drag.edge, Math.round(drag.t * 100) / 100);
          setDrag(null);
        }}
      />
      <p className="text-xs text-gray-500">
        Drag the green (start) or red (end) handle. On rebuild it snaps to the nearest real pause
        {drag ? ` — ${drag.edge} → ${fmtTime(drag.t)}` : ""}. Shaded = kept audio.
      </p>
    </div>
  );
}
