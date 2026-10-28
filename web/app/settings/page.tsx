"use client";

import { useEffect, useState } from "react";
import { api, upload } from "@/lib/api";

type Config = {
  style: {
    width: number;
    height: number;
    fps: number;
    min_clip_seconds: number;
    max_clip_seconds: number;
    default_clip_count: number;
    [k: string]: unknown;
  };
  brand_tag: string;
  credit_line: string;
  logo_path: string | null;
  bgm_path: string | null;
  sfx_path: string | null;
  sfx_on_transitions: boolean;
  profanity_mode: string;
  frame_rate_mode: string;
  grade: string;
  caption_font: string;
  caption_uppercase: boolean;
  caption_accent: string;
  face_recognition: boolean;
  broll_banned_content: string[];
  broll_reject_rules: string[];
};

type Status = {
  text: { ok: boolean; provider: string; model?: string; detail: string; fix?: string | null };
  vision: { ok: boolean; provider: string; detail: string; fix?: string | null };
  stock: { pexels: boolean; pixabay: boolean };
};

const Row = ({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) => (
  <label className="grid items-start gap-2 text-sm md:grid-cols-[220px_1fr]">
    <span className="pt-1 text-gray-300">
      {label}
      {hint && <span className="block text-xs text-gray-500">{hint}</span>}
    </span>
    <span>{children}</span>
  </label>
);

const input = "w-full rounded border border-ink-700 bg-ink-950 px-2 py-1";

export default function SettingsPage() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => {
    api<Config>("/api/settings").then(setCfg);
    api<Status>("/api/llm/status").then(setStatus).catch(() => null);
  };
  useEffect(load, []);

  async function save() {
    if (!cfg) return;
    setMsg(null);
    try {
      setCfg(await api<Config>("/api/settings", { method: "PUT", body: JSON.stringify(cfg) }));
      setMsg("Saved. Rebuild clips to apply.");
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function uploadFile(kind: "logo" | "music" | "sfx", f: File | undefined) {
    if (!f) return;
    setMsg(null);
    try {
      setCfg(await upload<Config>(`/api/settings/${kind}`, f));
      setMsg(`${kind} uploaded`);
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function remove(kind: "logo" | "music" | "sfx") {
    setCfg(await api<Config>(`/api/settings/${kind}`, { method: "DELETE" }));
  }

  if (!cfg) return <p className="text-sm text-gray-400">Loading…</p>;
  const set = (patch: Partial<Config>) => setCfg({ ...cfg, ...patch });
  const setStyle = (patch: Partial<Config["style"]>) => setCfg({ ...cfg, style: { ...cfg.style, ...patch } });
  const fileName = (p: string | null) => (p ? p.split(/[\\/]/).pop() : "none");

  return (
    <div className="max-w-3xl space-y-8" data-testid="settings">
      <h1 className="text-2xl font-bold">Settings</h1>

      {status && (
        <section className="space-y-1 rounded border border-ink-700 bg-ink-900 p-3 text-sm">
          <p>
            <b>Clip picker / copy:</b> {status.text.ok ? "✓" : "!"} {status.text.provider}
            {status.text.model ? ` · ${status.text.model}` : ""} — {status.text.detail}
          </p>
          {status.text.fix && <p className="text-yellow-300">Fix: {status.text.fix}</p>}
          <p>
            <b>B-roll vision:</b> {status.vision.ok ? "✓" : "–"} {status.vision.detail}
          </p>
          <p>
            <b>Stock footage keys:</b> Pexels {status.stock.pexels ? "set" : "not set"} · Pixabay{" "}
            {status.stock.pixabay ? "set" : "not set"}{" "}
            <span className="text-gray-500">(free keys go in .env: PEXELS_API_KEY, PIXABAY_API_KEY)</span>
          </p>
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Style preset</h2>
        <Row label="Output" hint="vertical 9:16">
          <select
            className={input}
            value={`${cfg.style.width}x${cfg.style.height}`}
            onChange={(e) => {
              const [w, h] = e.target.value.split("x").map(Number);
              setStyle({ width: w, height: h });
            }}
          >
            <option value="1080x1920">1080 × 1920 (default)</option>
            <option value="720x1280">720 × 1280 (faster previews)</option>
          </select>
        </Row>
        <Row label="Frame rate">
          <select className={input} value={cfg.style.fps} onChange={(e) => setStyle({ fps: Number(e.target.value) })}>
            <option value={60}>60 fps (default)</option>
            <option value={30}>30 fps</option>
          </select>
        </Row>
        <Row label="30 → 60 fps" hint="interpolation only if you enable it">
          <select className={input} value={cfg.frame_rate_mode} onChange={(e) => set({ frame_rate_mode: e.target.value })}>
            <option value="repeat">Frame repetition (default)</option>
            <option value="interpolate">Motion interpolation (can show artifacts)</option>
          </select>
        </Row>
        <Row label="Clip length (s)" hint="hard max 30 s">
          <span className="flex gap-2">
            <input className={input} type="number" min={5} max={30} value={cfg.style.min_clip_seconds} onChange={(e) => setStyle({ min_clip_seconds: Number(e.target.value) })} />
            <input className={input} type="number" min={5} max={30} value={cfg.style.max_clip_seconds} onChange={(e) => setStyle({ max_clip_seconds: Math.min(30, Number(e.target.value)) })} />
          </span>
        </Row>
        <Row label="Default clips per episode">
          <input className={input} type="number" min={1} max={20} value={cfg.style.default_clip_count} onChange={(e) => setStyle({ default_clip_count: Number(e.target.value) })} />
        </Row>
        <Row label="Colour grade" hint="one grade for every clip">
          <select className={input} value={cfg.grade} onChange={(e) => set({ grade: e.target.value })}>
            {["clean", "warm", "cool", "none"].map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </Row>
        <Row label="Profanity">
          <select className={input} value={cfg.profanity_mode} onChange={(e) => set({ profanity_mode: e.target.value })}>
            <option value="prefer_alternative_then_bleep">Different seam first, bleep only if needed (default)</option>
            <option value="bleep">Always bleep</option>
            <option value="allow">Leave it in (flagged)</option>
          </select>
        </Row>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Captions</h2>
        <Row label="Font"><input className={input} value={cfg.caption_font} onChange={(e) => set({ caption_font: e.target.value })} /></Row>
        <Row label="Uppercase"><input type="checkbox" checked={cfg.caption_uppercase} onChange={(e) => set({ caption_uppercase: e.target.checked })} /></Row>
        <Row label="Emphasis colour"><input type="color" value={cfg.caption_accent} onChange={(e) => set({ caption_accent: e.target.value })} /></Row>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Brand</h2>
        <Row label="Brand tag" hint="first Instagram tag"><input className={input} value={cfg.brand_tag} onChange={(e) => set({ brand_tag: e.target.value })} /></Row>
        <Row label="Credit line" hint="empty = none (default)"><input className={input} value={cfg.credit_line} onChange={(e) => set({ credit_line: e.target.value })} /></Row>
        <Row label="Logo (top right)" hint="PNG with transparency works best">
          <span className="flex items-center gap-2">
            <span className="text-xs text-gray-400">{fileName(cfg.logo_path)}</span>
            <input aria-label="Upload logo" type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => uploadFile("logo", e.target.files?.[0])} />
            {cfg.logo_path && <button className="text-xs underline" onClick={() => remove("logo")}>remove</button>}
          </span>
        </Row>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Audio</h2>
        <Row label="Royalty-free music bed" hint="ducked to 19 dB under the voice">
          <span className="flex items-center gap-2">
            <span className="text-xs text-gray-400">{fileName(cfg.bgm_path)}</span>
            <input aria-label="Upload music" type="file" accept="audio/*" onChange={(e) => uploadFile("music", e.target.files?.[0])} />
            {cfg.bgm_path && <button className="text-xs underline" onClick={() => remove("music")}>remove</button>}
          </span>
        </Row>
        <Row label="Transition SFX" hint="only on B-roll transitions, off by default">
          <span className="flex items-center gap-2">
            <span className="text-xs text-gray-400">{fileName(cfg.sfx_path)}</span>
            <input aria-label="Upload SFX" type="file" accept="audio/*" onChange={(e) => uploadFile("sfx", e.target.files?.[0])} />
            <label className="flex items-center gap-1 text-xs">
              <input type="checkbox" checked={cfg.sfx_on_transitions} onChange={(e) => set({ sfx_on_transitions: e.target.checked })} /> enabled
            </label>
          </span>
        </Row>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">B-roll rules</h2>
        <Row label="Banned content" hint="one per line; applied to every pick and re-checked before export">
          <textarea className={input} rows={4} value={cfg.broll_banned_content.join("\n")} onChange={(e) => set({ broll_banned_content: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) })} />
        </Row>
        <Row label="Always reject" hint="one per line">
          <textarea className={input} rows={6} value={cfg.broll_reject_rules.join("\n")} onChange={(e) => set({ broll_reject_rules: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) })} />
        </Row>
        <Row label="Recognise the host across camera angles" hint="downloads SFace once (38 MB)">
          <input type="checkbox" checked={cfg.face_recognition} onChange={(e) => set({ face_recognition: e.target.checked })} />
        </Row>
      </section>

      <div className="flex items-center gap-3">
        <button onClick={save} className="rounded bg-accent px-4 py-2 text-sm font-semibold text-black">
          Save settings
        </button>
        {msg && <span className="text-sm text-gray-300" data-testid="settings-msg">{msg}</span>}
      </div>
    </div>
  );
}
