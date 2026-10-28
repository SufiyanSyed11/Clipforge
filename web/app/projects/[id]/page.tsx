"use client";

import { useCallback, useEffect, useState } from "react";
import ClipCard from "@/components/ClipCard";
import JobLog from "@/components/JobLog";
import QAChecklist from "@/components/QAChecklist";
import { api, type Clip, type Project } from "@/lib/api";

export default function ProjectPage({ params }: { params: { id: string } }) {
  const [project, setProject] = useState<Project | null>(null);
  const [clips, setClips] = useState<Clip[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const p = await api<Project>(`/api/projects/${params.id}`);
      setProject(p);
      if (p.status === "clips_ready") setClips(await api<Clip[]>(`/api/projects/${params.id}/clips`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [params.id]);

  useEffect(() => {
    load();
  }, [load]);

  // While clips are being built, refresh every few seconds.
  const building = clips.some((c) => c.status === "picked" || c.status === "building");
  useEffect(() => {
    if (!building) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [building, load]);

  async function confirmHost(speaker: string) {
    setSaving(true);
    setError(null);
    try {
      setClips([]);
      setProject(
        await api<Project>(`/api/projects/${params.id}/host`, {
          method: "POST",
          body: JSON.stringify({ speaker }),
        }),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (!project) return <p className="text-sm text-gray-400">{error ?? "Loading…"}</p>;

  const latest = project.jobs?.[0];
  const processJob = project.jobs?.find((j) => j.kind === "process_project");
  const activeJob = latest && (latest.status === "queued" || latest.status === "running") ? latest : null;
  const stats = project.speakers?.stats ?? [];
  const suggestion = project.host_suggestion;
  const m = project.media_info;

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-bold">{project.name}</h1>
        <p className="mt-1 text-sm text-gray-400" data-testid="project-status">
          {project.status.replace("_", " ")}
          {m &&
            ` · ${(m.duration / 60).toFixed(1)} min` +
              (m.has_video ? ` · ${m.width}×${m.height} @ ${m.fps} fps` : " · audio only")}
        </p>
        {error && <p className="mt-2 text-sm text-red-400">{error}</p>}
      </section>

      {latest && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">
            {latest.kind === "pick_clips" ? "Clip picking" : "Processing"}
          </h2>
          <JobLog key={latest.id} jobId={latest.id} onEnd={() => load()} />
        </section>
      )}

      {stats.length > 0 && (
        <section className="space-y-3" data-testid="host-section">
          <h2 className="text-lg font-semibold">Who is the host?</h2>
          {suggestion && (
            <p className="text-sm text-gray-300">
              Suggested: <b>{suggestion.speaker}</b> ({suggestion.confidence} confidence —{" "}
              {suggestion.reasons.join(", ")})
              {project.speakers?.separation !== null &&
                project.speakers?.separation !== undefined &&
                ` · speaker separation ${project.speakers.separation.toFixed(2)}`}
            </p>
          )}
          <div className="grid gap-3 md:grid-cols-2">
            {stats.map((s) => {
              const isHost = project.host_confirmed && project.host_speaker === s.speaker;
              return (
                <div
                  key={s.speaker}
                  className={`rounded border p-3 text-sm ${
                    isHost ? "border-accent" : "border-ink-700"
                  } bg-ink-900`}
                >
                  <div className="flex items-center justify-between">
                    <b>
                      {s.speaker}
                      {suggestion?.speaker === s.speaker && !project.host_confirmed && " (suggested)"}
                      {isHost && " · HOST"}
                    </b>
                    <span className="text-gray-400">
                      {Math.round(s.talk_share * 100)}% talk · {s.questions} questions
                    </span>
                  </div>
                  <p className="mt-2 italic text-gray-300">“{s.sample}”</p>
                  <button
                    onClick={() => confirmHost(s.speaker)}
                    disabled={saving || isHost || !!activeJob}
                    className="mt-3 rounded bg-accent px-3 py-1 text-xs font-semibold text-black disabled:opacity-40"
                  >
                    {isHost ? "Host" : project.host_confirmed ? `Make ${s.speaker} the host` : `${s.speaker} is the host`}
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {processJob && processJob.status === "done" && (
        <details>
          <summary className="cursor-pointer text-sm text-gray-400">
            Ingest / transcript / speaker QA
          </summary>
          <div className="mt-2">
            <QAChecklist reports={processJob.qa} />
          </div>
        </details>
      )}

      {project.status === "clips_ready" && (
        <section className="space-y-4">
          <h2 className="text-lg font-semibold">
            {clips.length} ranked clip{clips.length === 1 ? "" : "s"}
            {clips.length < project.clip_count &&
              ` (asked for ${project.clip_count}; fewer good clips were found, none padded)`}
          </h2>
          {clips.map((c) => (
            <ClipCard key={c.id} clip={c} onChange={load} />
          ))}
        </section>
      )}
    </div>
  );
}
