import { fmtTime, type Clip } from "@/lib/api";

export default function ClipCard({ clip }: { clip: Clip }) {
  const c = clip.plan;
  const p = c.plan;
  return (
    <article
      data-testid="clip-card"
      className="space-y-3 rounded border border-ink-700 bg-ink-900 p-4 text-sm"
    >
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-lg font-bold">#{c.rank}</span>
        <span className="text-accent">
          Virality {p.virality_score}/10
        </span>
        <span className="text-gray-300">
          {c.duration.toFixed(1)} s{c.stitched ? " · stitched" : ""}
        </span>
        <span className="text-gray-400">
          {p.segments.map((s) => `${fmtTime(s.start)}–${fmtTime(s.end)}`).join(" + ")}
        </span>
      </header>

      <div className="space-y-1">
        <p>
          <span className="text-gray-400">Hook: </span>
          {p.hook}
        </p>
        <p>
          <span className="text-gray-400">Context: </span>
          {p.context}
        </p>
        <p>
          <span className="text-gray-400">Payoff: </span>
          {p.payoff}
        </p>
      </div>

      <div>
        <p className="text-gray-400">Why this clip</p>
        <ul className="list-disc pl-5">
          {p.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </div>

      <div>
        <p className="text-gray-400">Titles</p>
        <ul className="list-disc pl-5">
          {p.title_options.map((t) => (
            <li key={t}>{t}</li>
          ))}
        </ul>
      </div>

      <details>
        <summary className="cursor-pointer text-gray-400">Transcript of the clip</summary>
        {p.segments.map((s, i) => (
          <p key={i} className="mt-1">
            {i > 0 && <span className="text-yellow-300">[seam] </span>}
            {s.text}
          </p>
        ))}
      </details>

      {p.broll_beats.length > 0 && (
        <details>
          <summary className="cursor-pointer text-gray-400">B-roll beats (Phase 4)</summary>
          <ul className="list-disc pl-5">
            {p.broll_beats.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </details>
      )}

      {p.improvements && (
        <p>
          <span className="text-gray-400">Could be better: </span>
          {p.improvements}
        </p>
      )}

      <ul className="space-y-1" data-testid="clip-qa">
        <li className="text-green-400">✓ length within the 30 s hard max</li>
        {c.flags.map((f) => (
          <li key={f} className="text-yellow-300">
            ! {f}
          </li>
        ))}
        {p.risk_flags.map((r, i) => (
          <li key={i} className="text-red-400">
            ✗ {r.type.replace("_", " ")}: {r.detail}
            {r.at_seconds !== null && ` @ ${fmtTime(r.at_seconds)}`}
          </li>
        ))}
        {c.windows_are_approximate && (
          <li className="text-gray-400">
            · windows use whisper times; Phase 2 moves every cut to real silence
          </li>
        )}
      </ul>
    </article>
  );
}
