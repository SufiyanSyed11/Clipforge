export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type QACheck = {
  name: string;
  passed: boolean;
  detail: string;
  severity: "error" | "warn";
  at_seconds: number | null;
};

export type QAReport = { step: string; checks: QACheck[]; passed: boolean };

export type Job = {
  id: number;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  project_id: number | null;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  qa: QAReport[];
  error: string | null;
  attempts: number;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type JobEvent = {
  id: number;
  job_id: number;
  at: string;
  level: "info" | "warn" | "error";
  message: string;
  progress: number | null;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers:
      init?.body instanceof FormData
        ? init?.headers
        : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}
