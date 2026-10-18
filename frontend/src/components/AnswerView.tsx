import type { Answer, AnswerReference, Evidence } from "@/lib/api";

function snippet(ev: Evidence | undefined, start: number, end: number): string | null {
  if (!ev) return null;
  const lines = ev.content.split("\n");
  return lines.slice(start - ev.start_line, end - ev.start_line + 1).join("\n");
}

function lines(start: number, end: number) {
  return start === end ? `L${start}` : `L${start}-${end}`;
}

export function AnswerView({ answer }: { answer: Answer }) {
  const ledger = new Map(answer.evidence.map((e) => [e.id, e]));

  return (
    <article className="flex flex-col gap-4">
      <p className="font-medium">{answer.summary}</p>

      {answer.explanation.length > 0 && (
        <ul className="flex flex-col gap-1.5 text-sm">
          {answer.explanation.map((s, i) => (
            <li
              key={i}
              className={
                s.kind === "fact"
                  ? "border-l-2 border-green-600 pl-3"
                  : "border-l-2 border-dashed border-neutral-400 pl-3 italic text-neutral-600 dark:text-neutral-400"
              }
            >
              {s.text}
              {s.kind === "interpretation" && (
                <span className="ml-2 text-xs not-italic text-neutral-500">
                  {s.downgraded ? "interpretation (no valid citation)" : "interpretation"}
                </span>
              )}
              {s.evidence.map((id) => {
                const ev = ledger.get(id);
                return ev ? (
                  <a
                    key={id}
                    href={ev.url}
                    target="_blank"
                    rel="noreferrer"
                    title={`${ev.path} ${lines(ev.start_line, ev.end_line)}`}
                    className="ml-1.5 rounded bg-neutral-100 px-1 font-mono text-xs not-italic text-neutral-600 hover:underline dark:bg-neutral-800 dark:text-neutral-300"
                  >
                    {id}
                  </a>
                ) : null;
              })}
            </li>
          ))}
        </ul>
      )}

      <References title="Code references" refs={answer.references} ledger={ledger} />
      <References title="Related tests" refs={answer.tests} ledger={ledger} />

      {answer.gaps.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-800 dark:bg-amber-950/40">
          <p className="font-medium">Gaps</p>
          <ul className="list-disc pl-5">
            {answer.gaps.map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        </div>
      )}

      <details className="text-sm">
        <summary className="cursor-pointer text-neutral-500">
          Retrieved context ({answer.evidence.length} chunks)
        </summary>
        <ul className="mt-2 flex flex-col gap-1 font-mono text-xs">
          {answer.evidence.map((e) => (
            <li key={e.id} className="flex gap-2">
              <span className="w-8 text-neutral-500">{e.id}</span>
              <span className="w-12 text-neutral-500">{e.score?.toFixed(3)}</span>
              <a href={e.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
                {e.path}:{lines(e.start_line, e.end_line)}
                {e.symbol && <span className="text-neutral-500"> {e.symbol}</span>}
              </a>
            </li>
          ))}
        </ul>
      </details>

      <p className="text-xs text-neutral-500">
        {answer.mode} · {answer.usage.model} · {answer.usage.input_tokens} in /{" "}
        {answer.usage.output_tokens} out · {(answer.usage.latency_ms / 1000).toFixed(1)}s
      </p>
    </article>
  );
}

function References({
  title,
  refs,
  ledger,
}: {
  title: string;
  refs: AnswerReference[];
  ledger: Map<string, Evidence>;
}) {
  if (refs.length === 0) return null;
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-medium">{title}</h3>
      {refs.map((r) => {
        const code = snippet(ledger.get(r.evidence), r.start_line, r.end_line);
        return (
          <div
            key={`${r.path}:${r.start_line}-${r.end_line}`}
            className="flex flex-col gap-1 rounded-md border border-neutral-200 p-3 text-sm dark:border-neutral-800"
          >
            <div className="flex flex-wrap items-baseline gap-x-2">
              <a href={r.url} target="_blank" rel="noreferrer" className="font-mono text-xs hover:underline">
                {r.path}:{lines(r.start_line, r.end_line)}
              </a>
              {r.symbol && <span className="font-mono text-xs text-neutral-500">{r.symbol}</span>}
            </div>
            {r.note && <p className="text-neutral-600 dark:text-neutral-400">{r.note}</p>}
            {code && (
              <pre className="max-h-64 overflow-auto rounded bg-neutral-50 p-2 text-xs dark:bg-neutral-900">
                {code}
              </pre>
            )}
          </div>
        );
      })}
    </section>
  );
}
