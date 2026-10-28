"use client";

import { useEffect, useRef, useState } from "react";
import { API_URL, type Job, type JobEvent } from "@/lib/api";
import QAChecklist from "./QAChecklist";

/** Live job log over Server-Sent Events, then the job's QA checklist when it ends. */
type Props = { jobId: number; onEnd?: (job: Job) => void };

export default function JobLog(props: Props) {
  // A new job id starts from a clean log.
  return <JobLogStream key={props.jobId} {...props} />;
}

function JobLogStream({ jobId, onEnd }: Props) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const onEndRef = useRef(onEnd);
  useEffect(() => {
    onEndRef.current = onEnd;
  });

  useEffect(() => {
    const es = new EventSource(`${API_URL}/api/jobs/${jobId}/events`);
    es.addEventListener("log", (e) => {
      const ev = JSON.parse((e as MessageEvent).data) as JobEvent;
      setEvents((prev) => (prev.some((p) => p.id === ev.id) ? prev : [...prev, ev]));
    });
    es.addEventListener("end", (e) => {
      const j = JSON.parse((e as MessageEvent).data) as Job;
      setJob(j);
      es.close();
      onEndRef.current?.(j);
    });
    return () => es.close();
  }, [jobId]);

  const progress = [...events].reverse().find((e) => e.progress !== null)?.progress ?? null;
  const status = job?.status ?? (events.length ? "running" : "queued");

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 text-sm">
        <span>Job #{jobId}</span>
        <span
          data-testid="job-status"
          className={
            status === "done"
              ? "text-green-400"
              : status === "failed"
                ? "text-red-400"
                : "text-yellow-300"
          }
        >
          {status}
        </span>
        {progress !== null && status === "running" && (
          <div className="h-1.5 w-40 overflow-hidden rounded bg-ink-700">
            <div className="h-full bg-accent" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
        )}
      </div>
      <pre
        data-testid="job-log"
        className="max-h-72 overflow-auto rounded border border-ink-700 bg-black/60 p-3 text-xs leading-5"
      >
        {events.map((e) => (
          <div
            key={e.id}
            className={e.level === "error" ? "text-red-400" : e.level === "warn" ? "text-yellow-300" : ""}
          >
            <span className="text-gray-500">{e.at.slice(11, 19)} </span>
            {e.message}
          </div>
        ))}
      </pre>
      {job?.error && <p className="text-sm text-red-400">{job.error}</p>}
      {job && <QAChecklist reports={job.qa} />}
    </div>
  );
}
