"use client";

import { useState, type FormEvent, type ReactNode } from "react";

import {
  searchSemantic,
  searchSymbols,
  searchText,
  type ChunkHit,
  type SymbolHit,
  type TextSearchResult,
} from "@/lib/api";

type Mode = "text" | "symbols" | "semantic";

type Results =
  | { mode: "text"; data: TextSearchResult }
  | { mode: "symbols"; data: SymbolHit[] }
  | { mode: "semantic"; data: ChunkHit[] };

const MODES: { mode: Mode; label: string; placeholder: string }[] = [
  { mode: "text", label: "Text", placeholder: "validate_token" },
  { mode: "symbols", label: "Symbols", placeholder: "Context.invoke" },
  { mode: "semantic", label: "Semantic", placeholder: "where are retries handled" },
];

const inputClass =
  "rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700";

export function SearchPanel({ repoId }: { repoId: number }) {
  const [mode, setMode] = useState<Mode>("text");
  const [q, setQ] = useState("");
  const [regex, setRegex] = useState(false);
  const [glob, setGlob] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<Results | null>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    try {
      if (mode === "text") {
        setResults({ mode, data: await searchText(repoId, q, { regex, glob }) });
      } else if (mode === "symbols") {
        setResults({ mode, data: await searchSymbols(repoId, q.trim()) });
      } else {
        setResults({ mode, data: await searchSemantic(repoId, q.trim()) });
      }
    } catch (err) {
      setError((err as Error).message);
      setResults(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={onSubmit} className="flex flex-col gap-2">
        <div className="flex gap-1 text-sm">
          {MODES.map((m) => (
            <button
              key={m.mode}
              type="button"
              onClick={() => {
                setMode(m.mode);
                setResults(null);
                setError(null);
              }}
              className={`rounded-md px-3 py-1 ${
                mode === m.mode
                  ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
                  : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>
        <div className="flex gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={MODES.find((m) => m.mode === mode)?.placeholder}
            aria-label="Search query"
            spellCheck={false}
            className={`${inputClass} flex-1 ${mode === "semantic" ? "" : "font-mono text-sm"}`}
          />
          <button
            type="submit"
            disabled={loading || !q.trim()}
            className="rounded-md bg-neutral-900 px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          >
            {loading ? "Searching..." : "Search"}
          </button>
        </div>
        {mode === "text" && (
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={regex} onChange={(e) => setRegex(e.target.checked)} />
              Regex
            </label>
            <input
              value={glob}
              onChange={(e) => setGlob(e.target.value)}
              placeholder="Path glob, e.g. src/**/*.py"
              aria-label="Path glob"
              spellCheck={false}
              className={`${inputClass} flex-1 py-1 font-mono text-xs`}
            />
          </div>
        )}
      </form>

      {error && <p className="text-sm text-red-600">{error}</p>}
      {results && <ResultList results={results} />}
    </div>
  );
}

function ResultList({ results }: { results: Results }) {
  if (results.mode === "text") {
    const { matches, truncated } = results.data;
    return (
      <List count={matches.length} note={truncated ? "showing the first matches only" : undefined}>
        {matches.map((m) => (
          <li key={`${m.path}:${m.line}`} className="flex flex-col font-mono text-xs">
            <a href={m.url} target="_blank" rel="noreferrer" className="text-neutral-500 hover:underline">
              {m.path}:{m.line}
            </a>
            <code className="overflow-x-auto whitespace-pre">
              <Highlighted text={m.text} ranges={m.ranges} />
            </code>
          </li>
        ))}
      </List>
    );
  }
  if (results.mode === "symbols") {
    return (
      <List count={results.data.length}>
        {results.data.map((s) => (
          <li key={s.id} className="flex flex-col gap-0.5 text-sm">
            <div className="flex items-baseline gap-2">
              <span className="rounded bg-neutral-100 px-1.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                {s.kind}
              </span>
              <a href={s.url} target="_blank" rel="noreferrer" className="font-mono hover:underline">
                {s.qualified_name}
              </a>
              {s.is_test && <span className="text-xs text-neutral-500">test</span>}
            </div>
            <span className="font-mono text-xs text-neutral-500">
              {s.path}:{s.start_line}-{s.end_line}
            </span>
            {s.signature && (
              <code className="truncate font-mono text-xs text-neutral-600 dark:text-neutral-400">
                {s.signature}
              </code>
            )}
            {s.docstring && <p className="text-xs text-neutral-500">{s.docstring}</p>}
          </li>
        ))}
      </List>
    );
  }
  return (
    <List count={results.data.length}>
      {results.data.map((c) => (
        <li key={c.id} className="flex flex-col gap-1 text-sm">
          <div className="flex items-baseline gap-2 font-mono text-xs">
            <span className="text-neutral-500">{c.score.toFixed(3)}</span>
            <a href={c.url} target="_blank" rel="noreferrer" className="hover:underline">
              {c.path}:{c.start_line}-{c.end_line}
            </a>
            {c.symbol && <span className="text-neutral-500">{c.symbol}</span>}
          </div>
          <pre className="max-h-48 overflow-auto rounded bg-neutral-50 p-2 text-xs dark:bg-neutral-900">
            {c.content}
          </pre>
        </li>
      ))}
    </List>
  );
}

function List({ count, note, children }: { count: number; note?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-neutral-500">
        {count} result{count === 1 ? "" : "s"}
        {note && `, ${note}`}
      </p>
      {count > 0 && <ul className="flex flex-col gap-3">{children}</ul>}
    </div>
  );
}

function Highlighted({ text, ranges }: { text: string; ranges: [number, number][] }) {
  const parts: ReactNode[] = [];
  let at = 0;
  ranges.forEach(([s, e], i) => {
    if (s < at) return;
    parts.push(text.slice(at, s));
    parts.push(
      <mark key={i} className="rounded-sm bg-yellow-200 dark:bg-yellow-800/60 dark:text-inherit">
        {text.slice(s, e)}
      </mark>,
    );
    at = e;
  });
  parts.push(text.slice(at));
  return <>{parts}</>;
}
