"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import BrollReview from "@/components/BrollReview";
import CopyPackEditor from "@/components/CopyPackEditor";
import JobLog from "@/components/JobLog";
import QAChecklist from "@/components/QAChecklist";
import Waveform from "@/components/Waveform";
import { API_URL, CLIP_STATUS_LABEL, api, fmtTime, type ClipDetail } from "@/lib/api";

export default function ClipEditor({ params }: { params: { id: string; clipId: string } }) {
  const clipId = Number(params.clipId);
  const [clip, setClip] = useState<ClipDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false); // edits saved but not rebuilt
  const [captionDraft, setCaptionDraft] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    try {
      setClip(await api<ClipDetail>(`/api/clips/${clipId}`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [clipId]);

  useEffect(() => {
    load();
  }, [load]);

  const building = clip?.status === "building" || clip?.status === "picked";
  useEffect(() => {
    if (!building) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [building, load]);

  async function edit(body: Record<string, unknown>) {
    setError(null);
    try {
      setClip({ ...(await api<ClipDetail>(`/api/clips/${clipId}/edits`, { method: "PATCH", body: JSON.stringify(body) })), jobs: clip?.jobs ?? [] });
      setPending(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function exportClip() {
    setError(null);
    try {
      await api(`/api/clips/${clipId}/export`, { method: "POST" });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function rebuild() {
    setError(null);
    try {
      await api(`/api/clips/${clipId}/build`, { method: "POST" });
      setPending(false);
      setCaptionDraft({});
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (!clip) return <p className="text-sm text-gray-400">{error ?? "Loading…"}</p>;
  const spec = clip.spec;
  const plan = clip.plan.plan;
  const job = clip.jobs[0];
  // Current window per segment (from the last build's cuts), for the trim handles.
  const segments = plan.segments.map((s, i) => {
    const trims = (clip.edits.trims as Record<string, [number | null, number | null]> | undefined)?.[String(i)];
    return { start: trims?.[0] ?? s.start, end: trims?.[1] ?? s.end };
  });

  return (
    <div className="space-y-8">
      <section className="flex flex-wrap items-center gap-4">
        <Link href={`/projects/${params.id}`} className="text-sm text-gray-400 hover:text-white">
          ← Project
        </Link>
        <h1 className="text-2xl font-bold">Clip #{clip.rank}</h1>
        <span data-testid="editor-status" className="text-sm">
          {CLIP_STATUS_LABEL[clip.status]}
        </span>
        <button
          onClick={rebuild}
          disabled={building}
          className="rounded bg-accent px-4 py-2 text-sm font-semibold text-black disabled:opacity-40"
        >
          {pending ? "Apply edits & rebuild" : "Rebuild"}
        </button>
        {error && <span className="text-sm text-red-400">{error}</span>}
      </section>

      <section className="grid gap-6 md:grid-cols-[300px_1fr]">
        <div className="space-y-2">
          {clip.has_render ? (
            <video
              key={clip.updated_at ?? ""}
              src={`${API_URL}/api/clips/${clipId}/video?v=${encodeURIComponent(clip.updated_at ?? "")}`}
              controls
              playsInline
              className="aspect-[9/16] w-full rounded bg-black"
            />
          ) : (
            <div className="flex aspect-[9/16] w-full items-center justify-center rounded bg-black/60 text-gray-500">
              not rendered
            </div>
          )}
          {spec && (
            <p className="text-xs text-gray-400">
              {spec.width}×{spec.height} · {spec.fps} fps ·{" "}
              {spec.pieces.reduce((a, p) => a + p.src_end - p.src_start, 0).toFixed(2)} s
            </p>
          )}
          {clip.error && <p className="text-xs text-orange-300">{clip.error}</p>}
        </div>
        <div className="space-y-3">
          {job && (job.status === "queued" || job.status === "running") && <JobLog key={job.id} jobId={job.id} onEnd={load} />}
          <h2 className="text-lg font-semibold">QA</h2>
          <QAChecklist reports={clip.qa} />
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Trim (snaps to silence)</h2>
        <Waveform
          clipId={clipId}
          segments={segments}
          onTrim={(segment, edge, t) => edit({ trims: [{ segment, [edge]: t }] })}
        />
        {spec && (
          <ul className="text-xs text-gray-400">
            {spec.cuts.map((c, i) => (
              <li key={i}>
                {c.reason} {c.edge} at {fmtTime(c.src_time)} in silence {fmtTime(c.silence_from)}–
                {fmtTime(c.silence_to)}
                {c.extended_by > 0 && ` (moved ${c.extended_by.toFixed(2)} s to a natural pause)`}
              </li>
            ))}
          </ul>
        )}
        <button onClick={() => edit({ clear_trims: true })} className="rounded bg-ink-700 px-3 py-1 text-xs">
          Reset trims
        </button>
      </section>

      {spec && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">Captions</h2>
          <p className="text-xs text-gray-500">Fix any word, then rebuild. Censored words stay censored.</p>
          <div className="flex flex-wrap gap-1" data-testid="caption-words">
            {spec.captions.map((w, i) => (
              <input
                key={i}
                value={captionDraft[i] ?? w.text}
                disabled={w.censored}
                onChange={(e) => setCaptionDraft({ ...captionDraft, [i]: e.target.value })}
                className={`w-auto min-w-0 rounded border px-1 py-0.5 text-xs ${
                  w.emphasis ? "border-accent text-accent" : "border-ink-700"
                } bg-ink-950`}
                size={Math.max(3, (captionDraft[i] ?? w.text).length)}
              />
            ))}
          </div>
          <button
            disabled={!Object.keys(captionDraft).length}
            onClick={() =>
              edit({
                captions: Object.entries(captionDraft)
                  .filter(([, v]) => v.trim())
                  .map(([index, text]) => ({ index: Number(index), text })),
              })
            }
            className="rounded bg-ink-700 px-3 py-1 text-xs disabled:opacity-40"
          >
            Save caption fixes
          </button>
        </section>
      )}

      {spec && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">Host reaction insert</h2>
          <p className="text-sm text-gray-400">
            {spec.host_insert
              ? `${fmtTime(spec.host_insert.out_start)}–${fmtTime(spec.host_insert.out_end)}: ${spec.host_insert.reason}`
              : "None in this clip (host on camera, or no suitable listening shot)."}
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={(clip.edits.host_insert as boolean | undefined) ?? true}
              onChange={(e) => edit({ host_insert: e.target.checked })}
            />
            Use the host-reaction insert
          </label>
        </section>
      )}

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Copy pack</h2>
        <CopyPackEditor
          clipId={clipId}
          pack={clip.copy_pack as never}
          onSaved={load}
        />
      </section>

      <section className="space-y-2" data-testid="export-section">
        <h2 className="text-lg font-semibold">Export</h2>
        <p className="text-sm text-gray-400">
          Exports only clips that passed the verification gate, with B-roll checked against your current
          rules and a copy pack. The pack holds the video, transcript, shot list, captions, copy and QA report.
        </p>
        <div className="flex items-center gap-3">
          <button onClick={exportClip} disabled={building} className="rounded bg-accent px-3 py-1 text-sm font-semibold text-black disabled:opacity-40">
            Export pack
          </button>
          {clip.has_export && (
            <a href={`${API_URL}/api/clips/${clipId}/export`} className="text-sm text-accent underline" data-testid="download-export">
              Download .zip
            </a>
          )}
        </div>
        {(() => {
          const last = clip.jobs.find((j) => j.kind === "export_clip");
          if (!last) return null;
          if (last.status === "failed") return <p className="text-sm text-red-400">{last.error}</p>;
          if (last.status === "done" && (last.result as { unverified_test?: boolean })?.unverified_test)
            return <p className="text-sm text-orange-300">Test export (FAKE_MODELS): marked UNVERIFIED-TEST.</p>;
          return <p className="text-xs text-gray-500">last export: {last.status}</p>;
        })()}
      </section>

      {spec && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">B-roll (contact sheets)</h2>
          <BrollReview
            clipId={clipId}
            slots={spec.broll}
            onDecision={(slot, action, candidate) => edit({ broll: [{ slot, action, candidate }] })}
            onUploaded={() => {
              setPending(true);
              load();
            }}
          />
        </section>
      )}
    </div>
  );
}
