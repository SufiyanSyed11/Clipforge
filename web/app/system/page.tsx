"use client";

import { useEffect, useState } from "react";
import JobLog from "@/components/JobLog";
import { api, type Job } from "@/lib/api";

type Env = {
  os: string;
  cpu: string | null;
  cpu_cores: number;
  ram_gb: number | null;
  disk_free_gb: number;
  gpus: { name: string; vram_gb: number | null }[];
  defaults: {
    whisper: { model: string; device: string; compute_type: string; reason: string };
    diarization_backend: string;
  };
  problems: string[];
};

export default function SystemPage() {
  const [health, setHealth] = useState<string>("checking…");
  const [env, setEnv] = useState<Env | null>(null);
  const [envError, setEnvError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ ok: boolean; version: string }>("/api/health")
      .then((h) => setHealth(h.ok ? `API online (v${h.version})` : "API unhealthy"))
      .catch(() => setHealth("API offline — start it with scripts/dev"));
    api<Env>("/api/env")
      .then(setEnv)
      .catch((e: Error) => setEnvError(e.message));
  }, []);

  async function runPing() {
    setError(null);
    try {
      const job = await api<Job>("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ kind: "ping", payload: { steps: 3, delay: 0.5 } }),
      });
      setJobId(job.id);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-bold">System</h1>
        <p className="mt-1 text-sm text-gray-400" data-testid="api-health">
          {health}
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">End-to-end check</h2>
        <p className="text-sm text-gray-400">
          Sends a ping job through API → queue → worker and streams its log back here.
        </p>
        <button
          onClick={runPing}
          className="rounded bg-accent px-4 py-2 text-sm font-semibold text-black hover:bg-accent-soft"
        >
          Run ping job
        </button>
        {error && <p className="text-sm text-red-400">{error}</p>}
        {jobId !== null && <JobLog jobId={jobId} />}
      </section>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Hardware</h2>
        {envError && (
          <p className="text-sm text-yellow-300">
            {envError.includes("404") ? "Run `clipforge-check` to inspect this PC." : envError}
          </p>
        )}
        {env && (
          <div className="rounded border border-ink-700 bg-ink-900 p-4 text-sm">
            <p>{env.os}</p>
            <p>
              CPU: {env.cpu ?? "unknown"} ({env.cpu_cores} cores) · RAM: {env.ram_gb} GB · Free
              disk: {env.disk_free_gb} GB
            </p>
            <p>GPU: {env.gpus.map((g) => `${g.name} (${g.vram_gb} GB)`).join(", ") || "none"}</p>
            <p className="mt-2">
              Whisper: <b>{env.defaults.whisper.model}</b> on {env.defaults.whisper.device} (
              {env.defaults.whisper.compute_type}) — {env.defaults.whisper.reason}
            </p>
            <p>Diarization: {env.defaults.diarization_backend}</p>
            {env.problems.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-yellow-300">
                {env.problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
