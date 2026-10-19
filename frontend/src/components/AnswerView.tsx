"use client";

import { useState, type ReactNode } from "react";

import { Code } from "@/components/Code";
import type { Answer, AnswerReference, AnswerSentence, Evidence, FlowNode, Step } from "@/lib/api";

type Ledger = Map<string, Evidence>;

function lines(start: number, end: number) {
  return start === end ? `L${start}` : `L${start}-${end}`;
}

// The cited range when the evidence covers it, else the whole evidence range.
function snippet(ev: Evidence | undefined, start: number, end: number) {
  if (!ev) return null;
  if (start < ev.start_line || end > ev.end_line) return { code: ev.content, start: ev.start_line };
  const all = ev.content.split("\n");
  return { code: all.slice(start - ev.start_line, end - ev.start_line + 1).join("\n"), start };
}

function shortSymbol(symbol: string) {
  const parts = symbol.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : symbol;
}

export function AnswerView({ answer }: { answer: Answer }) {
  const ledger: Ledger = new Map(answer.evidence.map((e) => [e.id, e]));
  const facts = answer.explanation.filter((s) => s.kind === "fact").length;

  return (
    <article className="flex flex-col gap-6">
      <p className="text-base font-medium leading-relaxed">{answer.summary}</p>

      {answer.explanation.length > 0 && (
        <Section
          title="Explanation"
          aside={
            <span className="flex items-center gap-3">
              <span className="flex items-center gap-1.5">
                <span className="h-3 w-0.5 bg-green-600" />
                {facts} fact{facts === 1 ? "" : "s"}, cited from the code
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-3 border-l-2 border-dashed border-neutral-400" />
                {answer.explanation.length - facts} interpretation
                {answer.explanation.length - facts === 1 ? "" : "s"}
              </span>
            </span>
          }
        >
          <ul className="flex flex-col gap-2 text-sm leading-relaxed">
            {answer.explanation.map((s, i) => (
              <Sentence key={i} sentence={s} ledger={ledger} />
            ))}
          </ul>
        </Section>
      )}

      {answer.flow.length > 0 && (
        <Section title="Code flow">
          <Flow nodes={answer.flow} ledger={ledger} depth={0} />
        </Section>
      )}

      {answer.references.length > 0 && (
        <Section title="Code references" aside={`${answer.references.length}`}>
          <div className="flex flex-col gap-3">
            {answer.references.map((r) => (
              <ReferenceCard key={`${r.path}:${r.start_line}-${r.end_line}`} item={r} ledger={ledger} />
            ))}
          </div>
        </Section>
      )}

      {answer.tests.length > 0 && (
        <Section title="Related tests" aside={`${answer.tests.length}`}>
          <ul className="flex flex-col divide-y divide-neutral-200 rounded-md border border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
            {answer.tests.map((t) => (
              <TestRow key={`${t.path}:${t.start_line}-${t.end_line}`} item={t} ledger={ledger} />
            ))}
          </ul>
        </Section>
      )}

      {answer.gaps.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-800 dark:bg-amber-950/40">
          <p className="font-medium">What the evidence does not cover</p>
          <ul className="mt-1 list-disc pl-5">
            {answer.gaps.map((g, i) => (
              <li key={i}>{g}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-col gap-2 border-t border-neutral-200 pt-3 dark:border-neutral-800">
        {answer.trace.length > 0 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-neutral-500 hover:text-neutral-900 dark:hover:text-white">
              {answer.mode === "navigator"
                ? `Investigation trace (${answer.trace.length} step${answer.trace.length === 1 ? "" : "s"})`
                : "Retrieval step"}
            </summary>
            <div className="mt-2">
              <StepList steps={answer.trace} detailed />
            </div>
          </details>
        )}

        <details className="text-sm">
          <summary className="cursor-pointer text-neutral-500 hover:text-neutral-900 dark:hover:text-white">
            {answer.mode === "navigator"
              ? `Cited evidence (${answer.evidence.length} of ${answer.evidence_total} gathered)`
              : `Retrieved context (${answer.evidence.length} chunks)`}
          </summary>
          <ul className="mt-2 flex flex-col gap-1 font-mono text-xs">
            {answer.evidence.map((e) => (
              <li key={e.id} className="flex gap-2">
                <span className="w-8 shrink-0 text-neutral-500">{e.id}</span>
                {e.score != null && (
                  <span className="w-12 shrink-0 text-neutral-500">{e.score.toFixed(3)}</span>
                )}
                <a href={e.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
                  {e.path}:{lines(e.start_line, e.end_line)}
                  {e.symbol && <span className="text-neutral-500"> {e.symbol}</span>}
                </a>
              </li>
            ))}
          </ul>
        </details>

        <p className="text-xs text-neutral-500">
          {answer.mode === "navigator" ? "Navigator" : "Baseline"} · {answer.usage.model} ·{" "}
          {answer.usage.input_tokens.toLocaleString()} in
          {answer.usage.cache_read_tokens > 0 &&
            ` (+${answer.usage.cache_read_tokens.toLocaleString()} cached)`}{" "}
          / {answer.usage.output_tokens.toLocaleString()} out
          {answer.mode === "navigator" && ` · ${answer.usage.tool_calls} tool calls`} ·{" "}
          {(answer.usage.latency_ms / 1000).toFixed(1)}s
        </p>
      </div>
    </article>
  );
}

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">{title}</h3>
        {aside && <span className="text-xs text-neutral-500">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

function Sentence({ sentence: s, ledger }: { sentence: AnswerSentence; ledger: Ledger }) {
  const [open, setOpen] = useState<string | null>(null);
  const ev = open ? ledger.get(open) : undefined;
  const fact = s.kind === "fact";

  return (
    <li
      className={
        fact
          ? "border-l-2 border-green-600 pl-3"
          : "border-l-2 border-dashed border-neutral-400 pl-3 text-neutral-600 dark:text-neutral-400"
      }
    >
      <span className={fact ? "" : "italic"}>{s.text}</span>
      {!fact && (
        <span className="ml-2 text-xs text-neutral-500">
          {s.downgraded ? "interpretation, no valid citation" : "interpretation"}
        </span>
      )}
      {s.evidence.map((id) =>
        ledger.has(id) ? (
          <button
            key={id}
            type="button"
            onClick={() => setOpen(open === id ? null : id)}
            aria-expanded={open === id}
            title={`${ledger.get(id)!.path} ${lines(ledger.get(id)!.start_line, ledger.get(id)!.end_line)}`}
            className={`ml-1.5 rounded px-1 font-mono text-xs not-italic ${
              open === id
                ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
                : "bg-neutral-100 text-neutral-600 hover:bg-neutral-200 dark:bg-neutral-800 dark:text-neutral-300 dark:hover:bg-neutral-700"
            }`}
          >
            {id}
          </button>
        ) : null,
      )}
      {ev && (
        <div className="mt-2 flex flex-col gap-1 not-italic">
          <Location url={ev.url} path={ev.path} start={ev.start_line} end={ev.end_line} symbol={ev.symbol} />
          <Code code={ev.content} path={ev.path} startLine={ev.start_line} maxLines={15} />
        </div>
      )}
    </li>
  );
}

function Location({
  url,
  path,
  start,
  end,
  symbol,
}: {
  url: string;
  path: string;
  start: number;
  end: number;
  symbol?: string | null;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 font-mono text-xs">
      <a href={url} target="_blank" rel="noreferrer" className="hover:underline" title="Open on GitHub">
        {path}:{lines(start, end)}
      </a>
      {symbol && <span className="text-neutral-500">{symbol}</span>}
    </div>
  );
}

function ReferenceCard({ item: r, ledger }: { item: AnswerReference; ledger: Ledger }) {
  const code = snippet(ledger.get(r.evidence), r.start_line, r.end_line);
  return (
    <div className="flex flex-col gap-2 rounded-md border border-neutral-200 p-3 text-sm dark:border-neutral-800">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className="font-mono text-sm font-medium">{r.symbol ? shortSymbol(r.symbol) : r.path}</span>
        <a
          href={r.url}
          target="_blank"
          rel="noreferrer"
          className="text-xs text-neutral-500 hover:text-neutral-900 hover:underline dark:hover:text-white"
        >
          View on GitHub
        </a>
      </div>
      <Location url={r.url} path={r.path} start={r.start_line} end={r.end_line} symbol={r.symbol} />
      {r.note && <p className="text-neutral-600 dark:text-neutral-400">{r.note}</p>}
      {code && <Code code={code.code} path={r.path} startLine={code.start} maxLines={12} />}
    </div>
  );
}

function TestRow({ item: t, ledger }: { item: AnswerReference; ledger: Ledger }) {
  const [open, setOpen] = useState(false);
  const code = snippet(ledger.get(t.evidence), t.start_line, t.end_line);
  return (
    <li className="flex flex-col gap-2 p-3 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-mono text-xs font-medium">{t.symbol ? shortSymbol(t.symbol) : t.path}</span>
        <a href={t.url} target="_blank" rel="noreferrer" className="font-mono text-xs text-neutral-500 hover:underline">
          {t.path}:{lines(t.start_line, t.end_line)}
        </a>
        {code && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="ml-auto text-xs text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
          >
            {open ? "Hide code" : "Show code"}
          </button>
        )}
      </div>
      {t.note && <p className="text-neutral-600 dark:text-neutral-400">{t.note}</p>}
      {open && code && <Code code={code.code} path={t.path} startLine={code.start} maxLines={20} />}
    </li>
  );
}

function Flow({ nodes, ledger, depth }: { nodes: FlowNode[]; ledger: Ledger; depth: number }) {
  return (
    <ul className={`flex flex-col gap-1.5 ${depth > 0 ? "ml-2 border-l border-neutral-200 pl-4 dark:border-neutral-800" : ""}`}>
      {nodes.map((n, i) => (
        <FlowItem key={`${n.evidence}-${i}`} node={n} ledger={ledger} depth={depth} />
      ))}
    </ul>
  );
}

function FlowItem({ node: n, ledger, depth }: { node: FlowNode; ledger: Ledger; depth: number }) {
  const [expanded, setExpanded] = useState(depth < 3);
  const [showCode, setShowCode] = useState(false);
  const code = snippet(ledger.get(n.evidence), n.start_line, n.end_line);
  const hasChildren = n.children.length > 0;

  return (
    <li className="flex flex-col gap-1.5 text-sm">
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          disabled={!hasChildren}
          aria-label={expanded ? "Collapse" : "Expand"}
          className="mt-0.5 w-4 shrink-0 text-xs text-neutral-400 hover:text-neutral-900 disabled:invisible dark:hover:text-white"
        >
          {expanded ? "▾" : "▸"}
        </button>
        <div className="flex min-w-0 flex-col gap-0.5">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <a href={n.url} target="_blank" rel="noreferrer" className="font-mono text-xs font-medium hover:underline">
              {shortSymbol(n.symbol)}
            </a>
            <span className="font-mono text-xs text-neutral-500">
              {n.path}:{lines(n.start_line, n.end_line)}
            </span>
            {code && (
              <button
                type="button"
                onClick={() => setShowCode((v) => !v)}
                className="text-xs text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
              >
                {showCode ? "hide code" : "code"}
              </button>
            )}
            {!expanded && hasChildren && (
              <span className="text-xs text-neutral-500">
                {n.children.length} more step{n.children.length === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {n.note && <p className="text-neutral-600 dark:text-neutral-400">{n.note}</p>}
          {showCode && code && <Code code={code.code} path={n.path} startLine={code.start} maxLines={15} />}
        </div>
      </div>
      {expanded && hasChildren && <Flow nodes={n.children} ledger={ledger} depth={depth + 1} />}
    </li>
  );
}

export function StepList({ steps, detailed = false }: { steps: Step[]; detailed?: boolean }) {
  if (steps.length === 0) return null;
  return (
    <ol className="flex flex-col gap-1 text-sm">
      {steps.map((s) => (
        <li key={s.index} className="flex gap-2">
          <span className="w-5 shrink-0 text-right tabular-nums text-neutral-400">{s.index}</span>
          <div className="flex min-w-0 flex-col">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className={s.error ? "text-red-600" : ""}>{s.label}</span>
              {s.summary && <span className="text-neutral-500">{s.summary}</span>}
            </div>
            {detailed && (
              <span className="truncate font-mono text-xs text-neutral-500" title={JSON.stringify(s.input)}>
                {s.tool}({Object.entries(s.input)
                  .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                  .join(", ")}) · {s.duration_ms} ms
                {s.evidence.length > 0 && ` · ${s.evidence[0]}${s.evidence.length > 1 ? `..${s.evidence.at(-1)}` : ""}`}
              </span>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
