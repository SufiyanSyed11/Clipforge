"use client";

import { useEffect, useState } from "react";
import { API_URL, api } from "@/lib/api";

type Plan = {
  suggested_at: string;
  why: string;
  posts: { platform: string; video_id: string; publish_at: string; privacy: string }[];
  youtube: { connected: boolean; can_upload: boolean };
};

// <input type="datetime-local"> wants local time without a zone.
function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function PostingPanel({ clipId, canUpload }: { clipId: number; canUpload: boolean }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [when, setWhen] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    api<Plan>(`/api/clips/${clipId}/posting`)
      .then((p) => {
        setPlan(p);
        setWhen(toLocalInput(p.suggested_at));
      })
      .catch((e: Error) => setMsg(e.message));
  }, [clipId]);

  async function schedule() {
    setMsg(null);
    try {
      await api(`/api/clips/${clipId}/youtube`, {
        method: "POST",
        body: JSON.stringify({ publish_at: new Date(when).toISOString() }),
      });
      setMsg("Upload queued: private on YouTube until the scheduled time.");
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  if (!plan) return <p className="text-sm text-gray-400">{msg ?? "Loading…"}</p>;
  return (
    <div className="space-y-2 text-sm" data-testid="posting">
      <p>
        Suggested: <b>{new Date(plan.suggested_at).toLocaleString()}</b>{" "}
        <span className="text-gray-400">({plan.why})</span>
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <a href={`${API_URL}/api/clips/${clipId}/post.ics`} className="text-accent underline">
          Add to calendar (.ics)
        </a>
        {plan.youtube.can_upload ? (
          <>
            <input
              type="datetime-local"
              value={when}
              onChange={(e) => setWhen(e.target.value)}
              className="rounded bg-ink-700 px-2 py-1"
            />
            <button
              onClick={schedule}
              disabled={!canUpload}
              title={canUpload ? "" : "Only verified clips with a copy pack"}
              className="rounded bg-accent px-3 py-1 font-semibold text-black disabled:opacity-40"
            >
              Schedule on YouTube (private until then)
            </button>
          </>
        ) : (
          <span className="text-gray-400">
            Connect YouTube with upload permission on the Learning page to schedule uploads.
          </span>
        )}
      </div>
      {plan.posts.length > 0 && (
        <ul className="list-disc pl-5 text-gray-300">
          {plan.posts.map((p) => (
            <li key={p.video_id}>
              {p.platform}: {p.video_id} · {p.privacy} · {new Date(p.publish_at).toLocaleString()}
            </li>
          ))}
        </ul>
      )}
      {msg && <p className="text-gray-300">{msg}</p>}
    </div>
  );
}
