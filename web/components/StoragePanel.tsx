"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Usage = {
  total_gb: number;
  free_gb: number;
  min_free_gb: number;
  low: boolean;
  projects_gb: number;
  intermediates_gb: number;
  broll_cache_gb: number;
  exports_gb: number;
  per_project: { project_id: number; gb: number; intermediates_gb: number }[];
  freed_gb?: number;
};

export default function StoragePanel() {
  const [u, setU] = useState<Usage | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Usage>("/api/storage")
      .then(setU)
      .catch((e: Error) => setMsg(e.message));
  }, []);

  async function clean(body: Record<string, unknown>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<Usage>("/api/storage/cleanup", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setU(r);
      setMsg(`Freed ${r.freed_gb?.toFixed(2)} GB`);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!u) return <p className="text-sm text-gray-400">{msg ?? "Checking…"}</p>;
  return (
    <div className="space-y-3 rounded border border-ink-700 bg-ink-900 p-4 text-sm" data-testid="storage">
      <p className={u.low ? "text-red-400" : ""}>
        Free: <b>{u.free_gb.toFixed(1)} GB</b> of {u.total_gb.toFixed(0)} GB (ClipForge keeps at
        least {u.min_free_gb} GB free and refuses downloads/renders below that)
      </p>
      <p className="text-gray-300">
        Projects {u.projects_gb.toFixed(2)} GB (of which re-creatable{" "}
        {u.intermediates_gb.toFixed(2)} GB) · B-roll cache {u.broll_cache_gb.toFixed(2)} GB ·
        Exports {u.exports_gb.toFixed(2)} GB
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          disabled={busy}
          onClick={() => clean({ target: "intermediates" })}
          className="rounded bg-ink-700 px-3 py-1 hover:bg-ink-500 disabled:opacity-40"
        >
          Clear re-creatable files ({u.intermediates_gb.toFixed(2)} GB)
        </button>
        <button
          disabled={busy}
          onClick={() => clean({ target: "broll_cache" }, "Delete downloaded B-roll? Clips keep their renders; rebuilding re-downloads.")}
          className="rounded bg-ink-700 px-3 py-1 hover:bg-ink-500 disabled:opacity-40"
        >
          Clear B-roll cache
        </button>
      </div>
      <p className="text-xs text-gray-500">
        Re-creatable files are extracted audio and per-clip voice/mix stems. Source episodes,
        renders and export packs are never removed by these buttons.
      </p>
      {u.per_project.length > 0 && (
        <table className="w-full text-left text-xs">
          <thead className="text-gray-400">
            <tr>
              <th>Project</th>
              <th>Size</th>
              <th>Re-creatable</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {u.per_project.map((p) => (
              <tr key={p.project_id}>
                <td>#{p.project_id}</td>
                <td>{p.gb.toFixed(2)} GB</td>
                <td>{p.intermediates_gb.toFixed(2)} GB</td>
                <td className="text-right">
                  <button
                    disabled={busy}
                    onClick={() =>
                      clean(
                        { target: "project", project_id: p.project_id, confirm: true },
                        `Delete project #${p.project_id} and ALL its files (source, clips, renders)? Export packs in data/exports are kept. This cannot be undone.`,
                      )
                    }
                    className="text-red-400 hover:underline disabled:opacity-40"
                  >
                    delete project
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {msg && <p className="text-xs text-gray-300">{msg}</p>}
    </div>
  );
}
