"use client";

import Link from "next/link";
import { useState } from "react";
import QAChecklist from "@/components/QAChecklist";
import { API_URL, CLIP_STATUS_LABEL, api, fmtTime, type Clip } from "@/lib/api";

const STATUS_COLOR: Record<Clip["status"], string> = {
  picked: "text-gray-400",
  building: "text-yellow-300",
  ready: "text-green-400",
  needs_review: "text-orange-400",
  failed: "text-red-400",
};

export default function ClipCard({ clip, onChange }: { clip: Clip; onChange?: () => void }) {
  const c = clip.plan;
  const p = c.plan;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function rebuild() {
    setBusy(true);
    setErr(null);
    try {
      await api(`/api/clips/${clip.id}/build`, { method: "POST" });
      onChange?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article
      data-testid="clip-card"
      className="grid gap-4 rounded border border-ink-700 bg-ink-900 p-4 text-sm md:grid-cols-[240px_1fr]"
    >
      <div className="space-y-2">
        {clip.has_render ? (
          <video
            data-testid="clip-video"
            key={clip.updated_at ?? ""}
            src={`${API_URL}/api/clips/${clip.id}/video?v=${encodeURIComponent(clip.updated_at ?? "")}`}
            controls
            playsInline
            className="aspect-[9/16] w-full rounded bg-black"
          />
        ) : (
          <div className="flex aspect-[9/16] w-full items-center justify-center rounded bg-black/60 text-gray-500">
            {clip.status === "failed" ? "not rendered" : "rendering…"}
          </div>
        )}
        <div className="flex gap-2">
          <Link
            href={`/projects/${clip.project_id}/clips/${clip.id}`}
            className="rounded bg-ink-700 px-3 py-1 text-xs hover:bg-ink-500"
          >
            Edit
          </Link>
          <button
            onClick={rebuild}
            disabled={busy || clip.status === "building" || clip.status === "picked"}
            className="rounded bg-ink-700 px-3 py-1 text-xs hover:bg-ink-500 disabled:opacity-40"
          >
            Rebuild
          </button>
        </div>
        {err && <p className="text-xs text-red-400">{err}</p>}
      </div>

      <div className="space-y-3">
        <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <span className="text-lg font-bold">#{c.rank}</span>
          <span className="text-accent">Virality {p.virality_score}/10</span>
          <span className="text-gray-300">
            {c.duration.toFixed(1)} s{c.stitched ? " · stitched" : ""}
          </span>
          <span className="text-gray-400">
            {p.segments.map((s) => `${fmtTime(s.start)}–${fmtTime(s.end)}`).join(" + ")}
          </span>
          <span data-testid="clip-status" className={STATUS_COLOR[clip.status]}>
            {CLIP_STATUS_LABEL[clip.status]}
          </span>
        </header>
        {clip.error && <p className="text-xs text-orange-300">{clip.error}</p>}

        <div className="space-y-1">
          <p>
            <span className="text-gray-400">Hook: </span>
            {p.hook}
          </p>
          <p>
            <span className="text-gray-400">Context: </span>
            {p.context}
          </p>
          <p>
            <span className="text-gray-400">Payoff: </span>
            {p.payoff}
          </p>
        </div>

        <div>
          <p className="text-gray-400">Why this clip</p>
          <ul className="list-disc pl-5">
            {p.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>

        {clip.copy_pack && (
          <div className="rounded border border-ink-700 p-2">
            <p className="font-semibold">{String(clip.copy_pack.title)}</p>
            <p className="text-xs text-gray-400">{(clip.copy_pack.instagram_tags as string[]).join(" ")}</p>
          </div>
        )}

        <div>
          <p className="text-gray-400">Title ideas</p>
          <ul className="list-disc pl-5">
            {p.title_options.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </div>

        <ul className="space-y-1" data-testid="clip-qa">
          <li className="text-green-400">✓ length within the 30 s hard max</li>
          {c.flags.map((f) => (
            <li key={f} className="text-yellow-300">
              ! {f}
            </li>
          ))}
          {p.risk_flags.map((r, i) => (
            <li key={i} className="text-red-400">
              ✗ {r.type.replace("_", " ")}: {r.detail}
              {r.at_seconds !== null && ` @ ${fmtTime(r.at_seconds)}`}
            </li>
          ))}
        </ul>

        {clip.qa.length > 0 && (
          <details>
            <summary className="cursor-pointer text-gray-400">
              Cut / verification / render QA ({clip.qa.every((r) => r.passed) ? "all passed" : "has failures"})
            </summary>
            <div className="mt-2">
              <QAChecklist reports={clip.qa} />
            </div>
          </details>
        )}
      </div>
    </article>
  );
}
