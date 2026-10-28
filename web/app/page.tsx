"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Project } from "@/lib/api";

const STATUS_LABEL: Record<Project["status"], string> = {
  created: "queued",
  processing: "processing",
  awaiting_host: "confirm host",
  picking: "picking clips",
  clips_ready: "clips ready",
  failed: "failed",
};

export default function Home() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [mode, setMode] = useState<"url" | "file">("url");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [clipCount, setClipCount] = useState(8);
  const [rights, setRights] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Project[]>("/api/projects")
      .then(setProjects)
      .catch((e: Error) => setError(e.message));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!rights) {
      setError("Confirm you have the rights or permission to clip this content.");
      return;
    }
    const form = new FormData();
    form.append("rights_confirmed", "true");
    form.append("name", name);
    form.append("clip_count", String(clipCount));
    if (mode === "url") form.append("url", url);
    else if (file) form.append("file", file);
    setBusy(true);
    try {
      const p = await api<Project>("/api/projects", { method: "POST", body: form });
      router.push(`/projects/${p.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-8 md:grid-cols-[1fr_1fr]">
      <section>
        <h1 className="mb-4 text-2xl font-bold">New project</h1>
        <form onSubmit={submit} className="space-y-4 rounded border border-ink-700 bg-ink-900 p-4">
          <div className="flex gap-2 text-sm">
            {(["url", "file"] as const).map((m) => (
              <button
                type="button"
                key={m}
                onClick={() => setMode(m)}
                className={`rounded px-3 py-1 ${mode === m ? "bg-ink-700 text-white" : "text-gray-400"}`}
              >
                {m === "url" ? "YouTube link" : "Upload file"}
              </button>
            ))}
          </div>
          {mode === "url" ? (
            <input
              aria-label="Episode URL"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.youtube.com/watch?v=…"
              className="w-full rounded border border-ink-700 bg-ink-950 px-3 py-2 text-sm"
            />
          ) : (
            <input
              aria-label="Episode file"
              type="file"
              accept="video/*,audio/*"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="w-full text-sm"
            />
          )}
          <input
            aria-label="Project name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Project name (optional)"
            className="w-full rounded border border-ink-700 bg-ink-950 px-3 py-2 text-sm"
          />
          <label className="flex items-center gap-2 text-sm">
            Clips
            <input
              aria-label="Clip count"
              type="number"
              min={1}
              max={20}
              value={clipCount}
              onChange={(e) => setClipCount(Number(e.target.value))}
              className="w-20 rounded border border-ink-700 bg-ink-950 px-2 py-1"
            />
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={rights}
              onChange={(e) => setRights(e.target.checked)}
              className="mt-1"
            />
            <span>
              I have the rights or permission to clip and repost this content. Nothing is
              downloaded until this is confirmed.
            </span>
          </label>
          {error && <p className="text-sm text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={busy || !rights || (mode === "url" ? !url : !file)}
            className="rounded bg-accent px-4 py-2 text-sm font-semibold text-black disabled:opacity-40"
          >
            {busy ? "Creating…" : "Generate clips"}
          </button>
        </form>
      </section>

      <section>
        <h2 className="mb-4 text-2xl font-bold">Projects</h2>
        {projects.length === 0 ? (
          <p className="text-sm text-gray-400">No projects yet.</p>
        ) : (
          <ul className="space-y-2">
            {projects.map((p) => (
              <li key={p.id}>
                <Link
                  href={`/projects/${p.id}`}
                  className="flex items-center justify-between rounded border border-ink-700 bg-ink-900 px-4 py-3 hover:border-accent"
                >
                  <span className="truncate">{p.name}</span>
                  <span
                    className={`ml-3 shrink-0 text-xs ${
                      p.status === "failed"
                        ? "text-red-400"
                        : p.status === "clips_ready"
                          ? "text-green-400"
                          : "text-yellow-300"
                    }`}
                  >
                    {STATUS_LABEL[p.status]}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
