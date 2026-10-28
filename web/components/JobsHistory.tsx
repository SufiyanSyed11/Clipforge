"use client";

import { api, type Job } from "@/lib/api";

const COLOR: Record<string, string> = {
  done: "text-green-400",
  failed: "text-red-400",
  cancelled: "text-gray-400",
  running: "text-yellow-300",
  queued: "text-gray-300",
  cancel_requested: "text-yellow-300",
};

export default function JobsHistory({ jobs, onChange }: { jobs: Job[]; onChange: () => void }) {
  if (!jobs.length) return null;
  async function act(id: number, what: "cancel" | "retry") {
    try {
      await api(`/api/jobs/${id}/${what}`, { method: "POST" });
    } finally {
      onChange();
    }
  }
  return (
    <table className="w-full text-left text-xs" data-testid="jobs-history">
      <thead className="text-gray-500">
        <tr>
          <th className="py-1">Job</th>
          <th>Status</th>
          <th>Started</th>
          <th>Note</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {jobs.map((j) => (
          <tr key={j.id} className="border-t border-ink-700 align-top">
            <td className="py-1">
              #{j.id} {j.kind.replace("_", " ")}
              {j.payload?.clip_id ? ` (clip ${String(j.payload.clip_id)})` : ""}
            </td>
            <td className={COLOR[j.status] ?? ""}>{j.status}</td>
            <td className="text-gray-400">{j.started_at?.slice(11, 19) ?? "–"}</td>
            <td className="max-w-md truncate text-gray-400" title={j.error ?? ""}>
              {j.error ?? ""}
            </td>
            <td className="space-x-1 text-right">
              {(j.status === "queued" || j.status === "running") && (
                <button className="rounded bg-ink-700 px-2 py-0.5" onClick={() => act(j.id, "cancel")}>
                  cancel
                </button>
              )}
              {(j.status === "failed" || j.status === "cancelled") && (
                <button className="rounded bg-ink-700 px-2 py-0.5" onClick={() => act(j.id, "retry")}>
                  retry
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
