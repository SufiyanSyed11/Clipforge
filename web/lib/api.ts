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
  status: "queued" | "running" | "done" | "failed" | "cancelled" | "cancel_requested";
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

export type SpeakerStats = {
  speaker: string;
  talk_seconds: number;
  talk_share: number;
  turns: number;
  questions: number;
  first_spoke_at: number;
  sample: string;
};

export type Project = {
  id: number;
  name: string;
  source_type: "url" | "upload";
  source: string;
  rights_confirmed: boolean;
  status: "created" | "processing" | "awaiting_host" | "picking" | "clips_ready" | "failed";
  clip_count: number;
  media_info: {
    duration: number;
    fps: number | null;
    width: number | null;
    height: number | null;
    has_video: boolean;
  } | null;
  speakers: { backend: string; separation: number | null; stats: SpeakerStats[] } | null;
  host_speaker: string | null;
  host_suggestion: { speaker: string; confidence: "high" | "medium" | "low"; reasons: string[] } | null;
  host_confirmed: boolean;
  created_at: string;
  jobs?: Job[];
};

export type RiskFlag = { type: string; detail: string; at_seconds: number | null };

export type ClipPlan = {
  segments: { start: number; end: number; text: string }[];
  hook: string;
  context: string;
  payoff: string;
  virality_score: number;
  reasons: string[];
  title_options: string[];
  broll_beats: string[];
  risk_flags: RiskFlag[];
  improvements: string;
};

export type ClipStatus = "picked" | "building" | "ready" | "needs_review" | "failed";

export type Clip = {
  id: number;
  project_id: number;
  rank: number;
  plan: {
    rank: number;
    plan: ClipPlan;
    duration: number;
    stitched: boolean;
    flags: string[];
    windows_are_approximate: boolean;
  };
  qa: QAReport[];
  status: ClipStatus;
  error: string | null;
  verified: boolean;
  has_render: boolean;
  has_export: boolean;
  copy_pack: Record<string, unknown> | null;
  edits: Record<string, unknown>;
  updated_at: string | null;
};

export const CLIP_STATUS_LABEL: Record<ClipStatus, string> = {
  picked: "queued for build",
  building: "building…",
  ready: "verified",
  needs_review: "needs review (gate failed)",
  failed: "failed",
};

export function fmtTime(t: number): string {
  const m = Math.floor(t / 60);
  return `${m}:${(t % 60).toFixed(1).padStart(4, "0")}`;
}

export type CutInfo = {
  edge: "start" | "end";
  src_time: number;
  requested: number;
  reason: string;
  silence_from: number;
  silence_to: number;
  extended_by: number;
};

export type BrollChoice = {
  source: string;
  source_id: string;
  title: string;
  page_url: string;
  license: string;
  width: number;
  height: number;
  approved_by: string;
  flags: string[];
  verdict: Record<string, unknown>;
};

export type BrollSlot = {
  index: number;
  out_start: number;
  out_end: number;
  beat_text: string;
  queries: string[];
  named_person: string | null;
  status: "empty" | "filled" | "weak";
  weak_reason: string;
  choice: BrollChoice | null;
  candidates: BrollChoice[];
};

export type Spec = {
  duration?: number;
  fps: number;
  width: number;
  height: number;
  pieces: { src_start: number; src_end: number }[];
  cuts: CutInfo[];
  captions: { text: string; spoken: string; out_start: number; censored: boolean; emphasis: boolean }[];
  broll: BrollSlot[];
  host_insert: { out_start: number; out_end: number; reason: string } | null;
  host_insert_enabled: boolean;
};

export type ClipDetail = Clip & { spec: Spec | null; jobs: Job[] };

export async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  return api<T>(path, { method: "POST", body: form });
}
