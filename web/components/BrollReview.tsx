"use client";

import { useRef } from "react";
import { API_URL, fmtTime, upload, type BrollChoice, type BrollSlot } from "@/lib/api";

function Sheet({ clipId, slot, candidate }: { clipId: number; slot: number; candidate?: number }) {
  const q = candidate === undefined ? "" : `?candidate=${candidate}`;
  return (
    // Served by the local API; next/image optimisation doesn't apply.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={`${API_URL}/api/clips/${clipId}/broll/${slot}/sheet${q}`}
      alt="contact sheet"
      className="w-full rounded border border-ink-700"
    />
  );
}

function Meta({ c }: { c: BrollChoice }) {
  const issues = (c.verdict?.issues as string[] | undefined) ?? [];
  const banned = (c.verdict?.banned_seen as string[] | undefined) ?? [];
  return (
    <div className="text-xs text-gray-400">
      <p>
        {c.source} · {c.width}×{c.height} · {c.title.slice(0, 60)}
      </p>
      <p>{c.license}</p>
      {c.approved_by && <p className="text-green-400">approved by {c.approved_by}</p>}
      {[...c.flags, ...issues.map((i) => `vision: ${i}`), ...banned.map((b) => `banned: ${b}`)].map((f) => (
        <p key={f} className="text-orange-300">
          ! {f}
        </p>
      ))}
    </div>
  );
}

/** Contact-sheet review: swap, approve, clear or upload a manual cover for every slot. */
export default function BrollReview({
  clipId,
  slots,
  onDecision,
  onUploaded,
}: {
  clipId: number;
  slots: BrollSlot[];
  onDecision: (slot: number, action: "approve" | "clear" | "auto", candidate?: number) => void;
  onUploaded: () => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const target = useRef<number>(0);
  if (!slots.length) return <p className="text-sm text-gray-500">No B-roll slots in this clip.</p>;
  return (
    <div className="space-y-4" data-testid="broll-review">
      <input
        ref={fileRef}
        type="file"
        accept="video/*"
        className="hidden"
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          await upload(`/api/clips/${clipId}/broll/${target.current}/upload`, f);
          onUploaded();
        }}
      />
      {slots.map((s) => (
        <div key={s.index} className="rounded border border-ink-700 bg-ink-900 p-3 text-sm">
          <div className="mb-2 flex flex-wrap items-baseline gap-3">
            <b>Slot {s.index + 1}</b>
            <span className="text-gray-400">
              {fmtTime(s.out_start)}–{fmtTime(s.out_end)}
            </span>
            <span
              className={
                s.status === "filled" ? "text-green-400" : s.status === "weak" ? "text-orange-400" : "text-gray-400"
              }
            >
              {s.status === "filled" ? "filled" : s.status === "weak" ? s.weak_reason : "stays on the speaker"}
            </span>
          </div>
          <p className="mb-2 italic text-gray-300">“{s.beat_text}”</p>
          {s.queries.length > 0 && <p className="mb-2 text-xs text-gray-500">searched: {s.queries.join(" · ")}</p>}
          {s.status === "filled" && s.choice && (
            <div className="mb-3 grid gap-2 md:grid-cols-[320px_1fr]">
              <Sheet clipId={clipId} slot={s.index} />
              <Meta c={s.choice} />
            </div>
          )}
          {s.candidates.length > 0 && (
            <details open={s.status !== "filled"}>
              <summary className="cursor-pointer text-gray-400">Candidates ({s.candidates.length})</summary>
              <div className="mt-2 grid gap-3 md:grid-cols-2">
                {s.candidates.map((c, k) => (
                  <div key={k} className="space-y-1">
                    <Sheet clipId={clipId} slot={s.index} candidate={k} />
                    <Meta c={c} />
                    <button
                      className="rounded bg-accent px-2 py-1 text-xs font-semibold text-black"
                      onClick={() => onDecision(s.index, "approve", k)}
                    >
                      Use this
                    </button>
                  </div>
                ))}
              </div>
            </details>
          )}
          <div className="mt-2 flex gap-2">
            <button className="rounded bg-ink-700 px-2 py-1 text-xs" onClick={() => onDecision(s.index, "clear")}>
              Stay on speaker
            </button>
            <button className="rounded bg-ink-700 px-2 py-1 text-xs" onClick={() => onDecision(s.index, "auto")}>
              Automatic
            </button>
            <button
              className="rounded bg-ink-700 px-2 py-1 text-xs"
              onClick={() => {
                target.current = s.index;
                fileRef.current?.click();
              }}
            >
              Upload my own cover
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
