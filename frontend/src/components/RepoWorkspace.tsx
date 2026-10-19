"use client";

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { AnswerView, StepList } from "@/components/AnswerView";
import { SearchPanel } from "@/components/SearchPanel";
import {
  createConversation,
  getConversation,
  getHealth,
  listConversations,
  sendMessage,
  type Answer,
  type AskMode,
  type Conversation,
  type Health,
  type Message,
  type Repo,
  type Step,
} from "@/lib/api";

type Tab = "ask" | "search";

type Turn = { id: number; question: string; mode: AskMode; steps: Step[] } & (
  | { state: "pending" }
  | { state: "done"; answer: Answer }
  | { state: "error"; error: string }
);

function turnOf(m: Message): Turn {
  const base = { id: m.id, question: m.question, mode: m.mode, steps: m.trace };
  if (m.status === "done" && m.answer) return { ...base, state: "done", answer: m.answer };
  if (m.status === "running") {
    return { ...base, state: "error", error: "This question is still running elsewhere or was interrupted." };
  }
  return { ...base, state: "error", error: m.error ?? "This question failed." };
}

function setUrlConversation(id: number | null) {
  const url = new URL(window.location.href);
  if (id === null) url.searchParams.delete("c");
  else url.searchParams.set("c", String(id));
  window.history.replaceState(null, "", url);
}

export function RepoWorkspace({ repo, initialConversation }: { repo: Repo; initialConversation: number | null }) {
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
        <AskPanel repoId={repo.id} initialConversation={initialConversation} />
      </div>
      <div className={tab === "search" ? "" : "hidden"}>
        <SearchPanel repoId={repo.id} />
      </div>
    </div>
  );
}

function AskPanel({ repoId, initialConversation }: { repoId: number; initialConversation: number | null }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<AskMode>("navigator");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(initialConversation);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(initialConversation !== null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const lastTurn = useRef<HTMLDivElement | null>(null);
  const pending = turns.some((t) => t.state === "pending");

  function refreshList() {
    listConversations(repoId).then(setConversations, () => {});
  }

  useEffect(() => {
    listConversations(repoId).then(setConversations, () => {});
  }, [repoId]);

  useEffect(() => {
    if (initialConversation === null) return;
    getConversation(initialConversation).then(
      (c) => {
        setTurns(c.messages.map(turnOf));
        setLoading(false);
      },
      (e: Error) => {
        setLoadError(e.message);
        setConversationId(null);
        setUrlConversation(null);
        setLoading(false);
      },
    );
  }, [initialConversation]);

  useEffect(() => () => abort.current?.abort(), []);

  function openConversation(id: number | null) {
    abort.current?.abort();
    setTurns([]);
    setLoadError(null);
    setConversationId(id);
    setUrlConversation(id);
    if (id === null) return;
    setLoading(true);
    getConversation(id).then(
      (c) => {
        setTurns(c.messages.map(turnOf));
        setLoading(false);
      },
      (e: Error) => {
        setLoadError(e.message);
        setLoading(false);
      },
    );
  }

  async function ask() {
    const q = question.trim();
    if (!q || pending || loading) return;
    const id = Date.now();
    setTurns((prev) => [...prev, { id, question: q, mode, steps: [], state: "pending" }]);
    setQuestion("");
    requestAnimationFrame(() => lastTurn.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    const update = (fn: (t: Turn) => Turn) => setTurns((prev) => prev.map((t) => (t.id === id ? fn(t) : t)));
    const controller = new AbortController();
    abort.current = controller;
    try {
      let cid = conversationId;
      if (cid === null) {
        cid = (await createConversation(repoId)).id;
        setConversationId(cid);
        setUrlConversation(cid);
      }
      const started = sendMessage(
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
      // The first message gives the conversation its title.
      setTimeout(refreshList, 500);
      await started;
      refreshList();
    } catch (err) {
      if (controller.signal.aborted) {
        update((t) => (t.state === "pending" ? { ...t, state: "error", error: "Stopped before an answer." } : t));
        return;
      }
      update((t) => ({ ...t, state: "error", error: (err as Error).message }));
    }
  }

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    ask();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      ask();
    }
  }

  return (
    <div className="grid flex-1 gap-8 lg:grid-cols-[13rem_minmax(0,1fr)]">
      <aside className="flex flex-col gap-2 self-start text-sm max-lg:hidden lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:overflow-y-auto">
        <button
          type="button"
          onClick={() => openConversation(null)}
          disabled={conversationId === null && turns.length === 0}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-left hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          New conversation
        </button>
        <ul className="flex flex-col gap-0.5">
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => openConversation(c.id)}
                title={c.title ?? undefined}
                className={`w-full truncate rounded-md px-2 py-1.5 text-left ${
                  c.id === conversationId
                    ? "bg-neutral-100 font-medium dark:bg-neutral-800"
                    : "text-neutral-600 hover:bg-neutral-50 dark:text-neutral-400 dark:hover:bg-neutral-900"
                }`}
              >
                {c.title ?? "Untitled"}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <div className="flex min-w-0 flex-col gap-6">
        {conversations.length > 0 && (
          <select
            value={conversationId ?? ""}
            onChange={(e) => openConversation(e.target.value ? Number(e.target.value) : null)}
            aria-label="Conversation"
            className="rounded-md border border-neutral-300 bg-transparent px-2 py-1.5 text-sm lg:hidden dark:border-neutral-700"
          >
            <option value="">New conversation</option>
            {conversations.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title ?? "Untitled"}
              </option>
            ))}
          </select>
        )}

        <section className="flex flex-1 flex-col gap-10">
          {loading && <p className="text-sm text-neutral-500">Loading conversation...</p>}
          {loadError && <p className="text-sm text-red-600">Could not open the conversation: {loadError}</p>}
          {!loading && turns.length === 0 && (
            <p className="rounded-md border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-700">
              Ask how something works, where it lives, or what calls it. Navigator investigates with search and
              code-intelligence tools; Baseline answers from the top chunks found by semantic search. Follow-up
              questions keep the evidence gathered so far.
            </p>
          )}
          {turns.map((t, i) => (
            <div
              key={t.id}
              ref={i === turns.length - 1 ? lastTurn : undefined}
              className="flex scroll-mt-4 flex-col gap-4"
            >
              <div className="flex flex-col items-end gap-1">
                <p className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-neutral-100 px-3 py-2 text-sm dark:bg-neutral-800">
                  {t.question}
                </p>
                <span className="text-xs text-neutral-500">{t.mode === "navigator" ? "Navigator" : "Baseline"}</span>
              </div>
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

        <form
          onSubmit={onSubmit}
          className="sticky bottom-0 flex flex-col gap-2 border-t border-neutral-200 bg-background py-3 dark:border-neutral-800"
        >
          <div className="flex items-end gap-2">
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onKeyDown}
              rows={2}
              placeholder={turns.length > 0 ? "Ask a follow-up" : "How does retry handling work?"}
              aria-label="Question"
              className="flex-1 resize-none rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
            />
            {pending ? (
              <button
                type="button"
                onClick={() => abort.current?.abort()}
                className="rounded-md border border-neutral-300 px-4 py-2 dark:border-neutral-700"
              >
                Stop
              </button>
            ) : (
              <button
                type="submit"
                disabled={loading || !question.trim()}
                className="rounded-md bg-neutral-900 px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
              >
                Ask
              </button>
            )}
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
            <span className="ml-auto text-xs text-neutral-500">Enter to ask, Shift+Enter for a new line</span>
          </div>
        </form>
      </div>
    </div>
  );
}
