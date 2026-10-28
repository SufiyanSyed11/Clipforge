"use client";

import { useState } from "react";
import { api } from "@/lib/api";

type Pack = {
  title: string;
  youtube_description: string;
  instagram_caption: string;
  instagram_tags: string[];
  x_post: string;
  generated_by?: string;
  edited?: boolean;
};

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="rounded bg-ink-700 px-2 py-0.5 text-xs"
      onClick={async () => {
        await navigator.clipboard.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1200);
      }}
    >
      {done ? "copied" : "copy"}
    </button>
  );
}

export default function CopyPackEditor({
  clipId,
  pack,
  onSaved,
}: {
  clipId: number;
  pack: Pack | null;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Pack | null>(pack);
  const [shown, setShown] = useState<Pack | null>(pack);
  const [msg, setMsg] = useState<string | null>(null);
  if (pack !== shown) {
    // A new pack from the server (regenerated or saved) replaces the draft.
    setShown(pack);
    setDraft(pack);
  }

  async function save() {
    if (!draft) return;
    setMsg(null);
    try {
      await api(`/api/clips/${clipId}/copy`, { method: "PUT", body: JSON.stringify(draft) });
      setMsg("Saved");
      onSaved();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  async function regenerate() {
    setMsg(null);
    try {
      await api(`/api/clips/${clipId}/copy`, { method: "POST" });
      setMsg("Writing new copy…");
      onSaved();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  if (!draft)
    return (
      <div className="space-y-2 text-sm">
        <p className="text-gray-400">No copy yet (written after the first build).</p>
        <button onClick={regenerate} className="rounded bg-ink-700 px-3 py-1 text-xs">
          Write copy now
        </button>
        {msg && <p className="text-xs text-orange-300">{msg}</p>}
      </div>
    );

  const field = (label: string, key: keyof Pack, rows: number, hint: string) => (
    <label className="block space-y-1 text-sm">
      <span className="flex items-center gap-2 text-gray-400">
        {label} <span className="text-xs text-gray-500">({hint})</span>
        <CopyButton text={String(draft[key] ?? "")} />
      </span>
      <textarea
        aria-label={label}
        rows={rows}
        value={String(draft[key] ?? "")}
        onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
        className="w-full rounded border border-ink-700 bg-ink-950 p-2 text-sm"
      />
    </label>
  );

  return (
    <div className="space-y-3" data-testid="copy-pack">
      {field("Title", "title", 2, "curiosity gap, named person, quoted punchline, exactly one emoji")}
      {field("YouTube description", "youtube_description", 4, "emojis + a call to comment")}
      {field("Instagram caption", "instagram_caption", 3, "no hashtags in the text")}
      <label className="block space-y-1 text-sm">
        <span className="flex items-center gap-2 text-gray-400">
          Instagram tags <span className="text-xs text-gray-500">(brand tag first, max 5)</span>
          <CopyButton text={draft.instagram_tags.join(" ")} />
        </span>
        <input
          aria-label="Instagram tags"
          value={draft.instagram_tags.join(" ")}
          onChange={(e) => setDraft({ ...draft, instagram_tags: e.target.value.split(/\s+/).filter(Boolean) })}
          className="w-full rounded border border-ink-700 bg-ink-950 p-2 text-sm"
        />
      </label>
      {field("X post", "x_post", 2, "1-2 sentences, no emojis")}
      <div className="flex items-center gap-2">
        <button onClick={save} className="rounded bg-accent px-3 py-1 text-xs font-semibold text-black">
          Save copy
        </button>
        <button onClick={regenerate} className="rounded bg-ink-700 px-3 py-1 text-xs">
          Regenerate
        </button>
        <span className="text-xs text-gray-500">
          {draft.edited ? "edited by you · " : ""}
          {draft.generated_by ? `drafted by ${draft.generated_by}` : ""}
        </span>
      </div>
      {msg && <p className="text-xs text-orange-300">{msg}</p>}
    </div>
  );
}
