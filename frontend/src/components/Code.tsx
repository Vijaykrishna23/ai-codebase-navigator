"use client";

import { Fragment, useEffect, useState } from "react";

import { highlight, langOf } from "@/lib/highlight";

function dedent(lines: string[]): string[] {
  const indents = lines.filter((l) => l.trim()).map((l) => l.match(/^[ \t]*/)![0].length);
  const cut = indents.length ? Math.min(...indents) : 0;
  return cut ? lines.map((l) => l.slice(cut)) : lines;
}

export function Code({
  code,
  path,
  startLine,
  maxLines,
}: {
  code: string;
  path: string;
  startLine: number;
  maxLines?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const [html, setHtml] = useState<{ source: string; html: string } | null>(null);

  const all = dedent(code.replace(/\n$/, "").split("\n"));
  const clipped = maxLines !== undefined && !expanded && all.length > maxLines + 2;
  const source = (clipped ? all.slice(0, maxLines) : all).join("\n");
  const lang = langOf(path);

  useEffect(() => {
    if (!lang) return;
    let live = true;
    highlight(source, lang).then(
      (out) => live && setHtml({ source, html: out }),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [source, lang]);

  return (
    <div className="flex flex-col overflow-hidden rounded border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-900">
      <div
        className="code-lines max-h-96 overflow-auto py-2 text-xs"
        style={{ counterReset: `line ${startLine - 1}` }}
      >
        {html?.source === source ? (
          <div dangerouslySetInnerHTML={{ __html: html.html }} />
        ) : (
          <pre>
            <code>
              {source.split("\n").map((l, i) => (
                <Fragment key={i}>
                  {i > 0 && "\n"}
                  <span className="line">{l}</span>
                </Fragment>
              ))}
            </code>
          </pre>
        )}
      </div>
      {maxLines !== undefined && all.length > maxLines + 2 && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="border-t border-neutral-200 px-3 py-1 text-left text-xs text-neutral-500 hover:text-neutral-900 dark:border-neutral-800 dark:hover:text-white"
        >
          {expanded ? "Show less" : `Show all ${all.length} lines`}
        </button>
      )}
    </div>
  );
}
