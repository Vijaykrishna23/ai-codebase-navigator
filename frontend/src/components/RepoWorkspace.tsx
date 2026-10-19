"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";

import { AnswerView, StepList } from "@/components/AnswerView";
import { SearchPanel } from "@/components/SearchPanel";
import {
  createConversation,
  getHealth,
  sendMessage,
  type Answer,
  type AskMode,
  type Health,
  type Repo,
  type Step,
} from "@/lib/api";

type Tab = "ask" | "search";

type Turn = { id: number; question: string; mode: AskMode; steps: Step[] } & (
  | { state: "pending" }
  | { state: "done"; answer: Answer }
  | { state: "error"; error: string }
);

export function RepoWorkspace({ repo }: { repo: Repo }) {
  const [tab, setTab] = useState<Tab>("ask");
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    getHealth().then(setHealth, () => setHealth(null));
  }, []);

  if (repo.status !== "ready" && repo.status !== "embedding") {
    return (
      <p className="text-sm text-neutral-500">
        This repository is {repo.status}. Search and questions open once indexing finishes.
      </p>
    );
  }

  const warnings: string[] = [];
  if (health && !health.llm_key_set) {
    warnings.push("ANTHROPIC_API_KEY is not set, so questions cannot be answered yet.");
  }
  if (health && !health.embedding_key_set) {
    warnings.push("VOYAGE_API_KEY is not set, so semantic search and baseline answers are off.");
  }
  if (repo.stats.chunks === undefined) {
    warnings.push("This repository was indexed before search chunks existed. Re-index it.");
  } else if ((repo.stats.embedded ?? 0) === 0 && health?.embedding_key_set) {
    warnings.push("This repository has no embeddings yet. Run Embed on the repositories page.");
  }

  return (
    <div className="flex flex-1 flex-col gap-6">
      <nav className="flex gap-4 border-b border-neutral-200 text-sm dark:border-neutral-800">
        {(["ask", "search"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-1 pb-2 capitalize ${
              tab === t ? "border-neutral-900 font-medium dark:border-white" : "border-transparent text-neutral-500"
            }`}
          >
            {t}
          </button>
        ))}
      </nav>

      {warnings.length > 0 && (
        <ul className="flex flex-col gap-1 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-800 dark:bg-amber-950/40">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      <div className={tab === "ask" ? "flex flex-1 flex-col" : "hidden"}>
        <AskPanel repoId={repo.id} />
      </div>
      <div className={tab === "search" ? "" : "hidden"}>
        <SearchPanel repoId={repo.id} />
      </div>
    </div>
  );
}

function AskPanel({ repoId }: { repoId: number }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<AskMode>("navigator");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const abort = useRef<AbortController | null>(null);
  const pending = turns.some((t) => t.state === "pending");

  useEffect(() => () => abort.current?.abort(), []);

  function newConversation() {
    abort.current?.abort();
    setTurns([]);
    setConversationId(null);
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const q = question.trim();
    if (!q || pending) return;
    const id = Date.now();
    setTurns((prev) => [...prev, { id, question: q, mode, steps: [], state: "pending" }]);
    setQuestion("");
    const update = (fn: (t: Turn) => Turn) =>
      setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)));
    const controller = new AbortController();
    abort.current = controller;
    try {
      let cid = conversationId;
      if (cid === null) {
        cid = (await createConversation(repoId)).id;
        setConversationId(cid);
      }
      await sendMessage(
        cid,
        q,
        mode,
        {
          onStep: (step) => update((t) => ({ ...t, steps: [...t.steps, step] })),
          onAnswer: (answer) => update((t) => ({ ...t, state: "done", answer })),
          onError: (error) => update((t) => ({ ...t, state: "error", error })),
        },
        controller.signal,
      );
    } catch (err) {
      if (controller.signal.aborted) return;
      update((t) => ({ ...t, state: "error", error: (err as Error).message }));
    }
  }

  return (
    <div className="flex flex-1 flex-col gap-6">
      <section className="flex flex-1 flex-col gap-8">
        {turns.length === 0 && (
          <p className="rounded-md border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-700">
            Ask how something works, where it lives, or what calls it. Navigator investigates with
            search and code-intelligence tools; Baseline answers from the top chunks found by
            semantic search. Follow-up questions keep the evidence gathered so far.
          </p>
        )}
        {turns.map((t) => (
          <div key={t.id} className="flex flex-col gap-3">
            <p className="self-end rounded-md bg-neutral-100 px-3 py-2 text-sm dark:bg-neutral-800">
              {t.question}
              <span className="ml-2 text-xs text-neutral-500">{t.mode}</span>
            </p>
            {t.state === "pending" && (
              <div className="flex flex-col gap-2">
                <StepList steps={t.steps} />
                <p className="animate-pulse text-sm text-neutral-500">
                  {t.mode === "navigator" ? "Investigating..." : "Retrieving code and answering..."}
                </p>
              </div>
            )}
            {t.state === "error" && (
              <div className="flex flex-col gap-2">
                <StepList steps={t.steps} />
                <p className="text-sm text-red-600">{t.error}</p>
              </div>
            )}
            {t.state === "done" && <AnswerView answer={t.answer} />}
          </div>
        ))}
      </section>

      <form onSubmit={onSubmit} className="flex flex-col gap-2">
        <div className="flex gap-2">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="How does retry handling work?"
            aria-label="Question"
            className="flex-1 rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
          />
          <button
            type="submit"
            disabled={pending || !question.trim()}
            className="rounded-md bg-neutral-900 px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          >
            Ask
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-sm">
          <span className="text-neutral-500">Mode</span>
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={mode === "navigator"} onChange={() => setMode("navigator")} />
            Navigator
          </label>
          <label className="flex items-center gap-1.5">
            <input type="radio" checked={mode === "baseline"} onChange={() => setMode("baseline")} />
            Baseline (RAG)
          </label>
          {turns.length > 0 && (
            <button
              type="button"
              onClick={newConversation}
              className="ml-auto text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
            >
              New conversation
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
