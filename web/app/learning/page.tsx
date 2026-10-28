"use client";

import { useEffect, useState } from "react";
import JobLog from "@/components/JobLog";
import { api, upload, type Job } from "@/lib/api";

type Insights = {
  updated_at: string;
  source: string;
  window_days: number;
  videos: number;
  best_length: string | null;
  title_patterns: Record<string, number>;
  top_topics: string[];
  best_hours: number[];
  top_titles: string[];
  summary: string;
};

type Learning = {
  insights: Insights | null;
  youtube: { connected: boolean; can_upload: boolean };
  youtube_configured: boolean;
  days: number;
  prompt_context: string;
};

export default function LearningPage() {
  const [data, setData] = useState<Learning | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [days, setDays] = useState(3);
  const [allowUpload, setAllowUpload] = useState(false);

  async function load() {
    try {
      const d = await api<Learning>("/api/learning");
      setData(d);
      setDays(d.days);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  useEffect(() => {
    // Fetch on mount: state is only set after the request resolves.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, []);

  async function run(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const connect = () =>
    run(async () => {
      const r = await api<{ auth_url: string }>("/api/learning/youtube/connect", {
        method: "POST",
        body: JSON.stringify({ allow_upload: allowUpload }),
      });
      window.location.href = r.auth_url;
    });

  const disconnect = () =>
    run(async () => setData(await api<Learning>("/api/learning/youtube/disconnect", { method: "POST" })));

  const refresh = () =>
    run(async () => {
      const job = await api<Job>("/api/learning/refresh", {
        method: "POST",
        body: JSON.stringify({ days }),
      });
      setJobId(job.id);
    });

  const importCsv = (f: File) =>
    run(async () => setData(await upload<Learning>(`/api/learning/csv?days=${days}`, f)));

  if (!data) return <p className="text-sm text-gray-400">{err ?? "Loading…"}</p>;
  const ins = data.insights;
  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-bold">Learning</h1>
        <p className="mt-1 text-sm text-gray-400">
          What worked on your channel in the last few days. Used as a tie-breaker by the clip
          picker and the copy writer — never over the clip rules (30 s max, complete story,
          hook in the first second).
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Data source</h2>
        <label className="flex items-center gap-2 text-sm">
          Look back
          <input
            type="number"
            min={1}
            max={30}
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="w-16 rounded bg-ink-700 px-2 py-1"
          />
          days
        </label>
        <div className="rounded border border-ink-700 bg-ink-900 p-4 text-sm space-y-2">
          <p className="font-semibold">YouTube (your own channel, read-only by default)</p>
          {!data.youtube_configured ? (
            <p className="text-yellow-300">
              Not set up: add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET (Google Cloud OAuth
              client, type Desktop app) to .env and restart. Or import a CSV below.
            </p>
          ) : data.youtube.connected ? (
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-green-400" data-testid="yt-connected">
                ✓ connected{data.youtube.can_upload ? " (can schedule private uploads)" : " (read-only)"}
              </span>
              <button onClick={refresh} className="rounded bg-accent px-3 py-1 font-semibold text-black">
                Refresh insights
              </button>
              <button onClick={disconnect} className="rounded bg-ink-700 px-3 py-1 hover:bg-ink-500">
                Disconnect
              </button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={allowUpload} onChange={(e) => setAllowUpload(e.target.checked)} />
                also allow scheduled private uploads
              </label>
              <button onClick={connect} className="rounded bg-accent px-3 py-1 font-semibold text-black">
                Connect YouTube
              </button>
            </div>
          )}
        </div>
        <div className="rounded border border-ink-700 bg-ink-900 p-4 text-sm space-y-2">
          <p className="font-semibold">CSV import (YouTube Studio or vidIQ export)</p>
          <p className="text-gray-400">Needs at least a title and a views column.</p>
          <input
            type="file"
            accept=".csv,text/csv"
            data-testid="csv-input"
            onChange={(e) => e.target.files?.[0] && importCsv(e.target.files[0])}
          />
        </div>
        {err && <p className="text-sm text-red-400">{err}</p>}
        {jobId !== null && <JobLog jobId={jobId} onEnd={() => load()} />}
      </section>

      <section className="space-y-2" data-testid="insights">
        <h2 className="text-lg font-semibold">What worked</h2>
        {!ins ? (
          <p className="text-sm text-gray-400">No data yet.</p>
        ) : (
          <div className="rounded border border-ink-700 bg-ink-900 p-4 text-sm space-y-2">
            <p className="text-gray-400">
              {ins.videos} uploads · {ins.source} · updated {new Date(ins.updated_at).toLocaleString()}
            </p>
            <p>{ins.summary}</p>
            {Object.keys(ins.title_patterns).length > 0 && (
              <ul className="list-disc pl-5">
                {Object.entries(ins.title_patterns).map(([k, lift]) => (
                  <li key={k} className={lift > 0 ? "text-green-400" : "text-orange-300"}>
                    {k}: {lift > 0 ? "+" : ""}
                    {Math.round(lift * 100)} pts more common in the top half
                  </li>
                ))}
              </ul>
            )}
            {ins.best_hours.length > 0 && (
              <p>Best upload hours (UTC): {ins.best_hours.map((h) => `${h}:00`).join(", ")}</p>
            )}
            {data.prompt_context ? (
              <p className="text-xs text-gray-500">In use by the picker and copy writer.</p>
            ) : (
              <p className="text-xs text-yellow-300">
                Not used in prompts (older than 7 days or too few uploads).
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
