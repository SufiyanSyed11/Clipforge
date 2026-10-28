import type { QAReport } from "@/lib/api";

function fmtTime(t: number | null) {
  if (t === null) return "";
  const m = Math.floor(t / 60);
  const s = (t % 60).toFixed(2).padStart(5, "0");
  return ` @ ${m}:${s}`;
}

export default function QAChecklist({ reports }: { reports: QAReport[] }) {
  if (!reports.length) return null;
  return (
    <div className="space-y-3" data-testid="qa-checklist">
      {reports.map((r) => (
        <div key={r.step} className="rounded border border-ink-700 bg-ink-900 p-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <span className={r.passed ? "text-green-400" : "text-red-400"}>
              {r.passed ? "QA passed" : "QA failed"}
            </span>
            <span className="text-gray-400">· {r.step}</span>
          </div>
          <ul className="space-y-1 text-sm">
            {r.checks.map((c, i) => (
              <li key={i} className="flex gap-2">
                <span
                  className={
                    c.passed ? "text-green-400" : c.severity === "warn" ? "text-yellow-400" : "text-red-400"
                  }
                >
                  {c.passed ? "✓" : c.severity === "warn" ? "!" : "✗"}
                </span>
                <span>
                  {c.name}
                  {c.detail && <span className="text-gray-400"> — {c.detail}</span>}
                  <span className="text-gray-500">{fmtTime(c.at_seconds)}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
