"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  ACTIVE_STATUSES,
  createRepo,
  deleteRepo,
  embedRepo,
  listRepos,
  reindexRepo,
  type Repo,
  type RepoStatus,
} from "@/lib/api";

const POLL_MS = 1500;

const STAGES: { status: RepoStatus; label: string }[] = [
  { status: "queued", label: "Queued" },
  { status: "cloning", label: "Cloning" },
  { status: "parsing", label: "Parsing" },
  { status: "embedding", label: "Embedding" },
  { status: "ready", label: "Ready" },
];

const LANGUAGE_LABELS: Record<string, string> = {
  python: "Python",
  javascript: "JavaScript",
  typescript: "TypeScript",
  tsx: "TSX",
};

export function RepoIngest() {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const refresh = useCallback(
    () =>
      listRepos().then(
        (data) => {
          setRepos(data);
          setLoadError(null);
        },
        (e: Error) => setLoadError(e.message),
      ),
    [],
  );

  // Load once, then poll while any ingest is still running.
  const loaded = repos !== null;
  const hasActive = repos?.some((r) => ACTIVE_STATUSES.includes(r.status)) ?? false;
  useEffect(() => {
    if (!loaded) {
      listRepos().then(setRepos, (e: Error) => setLoadError(e.message));
      return;
    }
    if (!hasActive) return;
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [loaded, hasActive, refresh]);

  const upsert = (repo: Repo) =>
    setRepos((prev) => [repo, ...(prev ?? []).filter((r) => r.id !== repo.id)]);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      upsert(await createRepo(url.trim()));
      setUrl("");
    } catch (err) {
      setSubmitError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  async function act(action: () => Promise<unknown>) {
    try {
      await action();
    } catch (err) {
      setLoadError((err as Error).message);
    }
    await refresh();
  }

  return (
    <>
      <form onSubmit={onSubmit} className="flex flex-col gap-2">
        <div className="flex gap-2">
          <input
            type="text"
            name="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            aria-label="GitHub repository URL"
            autoComplete="off"
            spellCheck={false}
            className="flex-1 rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
          />
          <button
            type="submit"
            disabled={submitting || !url.trim()}
            className="rounded-md bg-neutral-900 px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          >
            {submitting ? "Adding..." : "Ingest"}
          </button>
        </div>
        {submitError && <p className="text-sm text-red-600">{submitError}</p>}
      </form>

      <section className="flex flex-col gap-3">
        <h2 className="font-medium">Indexed repositories</h2>
        {loadError && <p className="text-sm text-red-600">{loadError}</p>}
        {repos === null && !loadError && (
          <p className="text-sm text-neutral-500">Loading...</p>
        )}
        {repos?.length === 0 && (
          <p className="text-sm text-neutral-500">None yet. Paste a GitHub URL above.</p>
        )}
        <ul className="flex flex-col gap-3">
          {repos?.map((repo) => (
            <RepoCard
              key={repo.id}
              repo={repo}
              onReindex={() => act(() => reindexRepo(repo.id))}
              onEmbed={() => act(() => embedRepo(repo.id))}
              onDelete={() => {
                if (confirm(`Remove ${repo.owner}/${repo.name} and its index?`)) {
                  act(() => deleteRepo(repo.id));
                }
              }}
              onRetry={() => act(() => createRepo(repo.url))}
            />
          ))}
        </ul>
      </section>
    </>
  );
}

function RepoCard({
  repo,
  onReindex,
  onEmbed,
  onDelete,
  onRetry,
}: {
  repo: Repo;
  onReindex: () => void;
  onEmbed: () => void;
  onDelete: () => void;
  onRetry: () => void;
}) {
  const active = ACTIVE_STATUSES.includes(repo.status);
  const { stats } = repo;
  const canEmbed =
    repo.status === "ready" &&
    stats.chunks !== undefined &&
    (stats.embedded ?? 0) < stats.chunks;

  return (
    <li className="flex flex-col gap-3 rounded-md border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col">
          <a
            href={repo.url}
            target="_blank"
            rel="noreferrer"
            className="truncate font-medium hover:underline"
          >
            {repo.owner}/{repo.name}
          </a>
          {repo.sha && (
            <span className="font-mono text-xs text-neutral-500">
              {repo.default_branch ? `${repo.default_branch} @ ` : ""}
              <a
                href={`${repo.url}/tree/${repo.sha}`}
                target="_blank"
                rel="noreferrer"
                className="hover:underline"
              >
                {repo.sha.slice(0, 7)}
              </a>
            </span>
          )}
        </div>
        <StatusBadge status={repo.status} />
      </div>

      {active && <Progress repo={repo} />}

      {repo.status === "failed" && repo.error && (
        <p className="text-sm text-red-600">{repo.error}</p>
      )}

      {repo.status === "ready" && stats.chunks === undefined && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          Indexed before search chunks existed. Re-index to enable semantic search.
        </p>
      )}

      {repo.status === "ready" && stats.chunks !== undefined && stats.edges === undefined && !stats.linking && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          Indexed before code intelligence existed. Re-index to build call, import and test links.
        </p>
      )}

      {repo.status === "ready" && stats.linking === "failed" && stats.linking_note && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          Linking failed: {stats.linking_note}
        </p>
      )}

      {repo.status === "ready" && stats.embedding_note && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          Embedding {stats.embedding}: {stats.embedding_note}
        </p>
      )}

      {repo.status === "ready" && (
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
          <Stat label="Source files" value={stats.source_files} />
          <Stat label="Tests" value={stats.test_files} />
          <Stat label="Symbols" value={stats.symbols} />
          <Stat label="Chunks" value={stats.chunks} />
          <Stat label="Links" value={stats.edges} />
          {stats.chunks !== undefined && (
            <Stat label="Embedded" value={`${stats.embedded ?? 0}/${stats.chunks}`} />
          )}
          <Stat
            label="Languages"
            value={Object.keys(stats.languages ?? {})
              .map((l) => LANGUAGE_LABELS[l] ?? l)
              .join(", ")}
          />
          {stats.duration_s !== undefined && (
            <Stat label="Took" value={`${stats.duration_s}s`} />
          )}
        </dl>
      )}

      <div className="flex gap-3 text-sm">
        {repo.status === "ready" && (
          <Link
            href={`/repos/${repo.id}`}
            className="rounded-md bg-neutral-900 px-3 py-1 text-white dark:bg-white dark:text-neutral-900"
          >
            Ask questions
          </Link>
        )}
        {repo.status === "failed" && (
          <button onClick={onRetry} className="text-neutral-700 hover:underline dark:text-neutral-300">
            Retry
          </button>
        )}
        {canEmbed && (
          <button onClick={onEmbed} className="text-neutral-700 hover:underline dark:text-neutral-300">
            Embed
          </button>
        )}
        {repo.status === "ready" && (
          <button onClick={onReindex} className="text-neutral-500 hover:underline">
            Re-index
          </button>
        )}
        <button onClick={onDelete} className="ml-auto text-neutral-500 hover:text-red-600 hover:underline">
          Remove
        </button>
      </div>
    </li>
  );
}

function StatusBadge({ status }: { status: RepoStatus }) {
  const styles: Record<RepoStatus, string> = {
    queued: "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
    cloning: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
    parsing: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
    embedding: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
    ready: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
    failed: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  };
  return (
    <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium capitalize ${styles[status]}`}>
      {status}
    </span>
  );
}

function Progress({ repo }: { repo: Repo }) {
  const current = STAGES.findIndex((s) => s.status === repo.status);
  const { parsed = 0, source_files, embedded = 0, chunks } = repo.stats;
  const [done, total, unit] =
    repo.status === "embedding" ? [embedded, chunks, "chunks"] : [parsed, source_files, "files"];
  const pct =
    (repo.status === "parsing" || repo.status === "embedding") && total
      ? Math.round((done / total) * 100)
      : null;

  return (
    <div className="flex flex-col gap-2">
      <ol className="flex gap-2 text-xs">
        {STAGES.map((stage, i) => (
          <li
            key={stage.status}
            className={
              i < current
                ? "text-neutral-500 line-through"
                : i === current
                  ? "font-medium"
                  : "text-neutral-400"
            }
          >
            {stage.label}
            {i < STAGES.length - 1 && <span className="ml-2 text-neutral-300">/</span>}
          </li>
        ))}
      </ol>
      {pct !== null && (
        <div className="flex items-center gap-3 text-xs text-neutral-500">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800">
            <div className="h-full bg-blue-600 transition-all" style={{ width: `${pct}%` }} />
          </div>
          <span className="font-mono">
            {done}/{total} {unit}
          </span>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number | undefined }) {
  if (value === undefined || value === "") return null;
  return (
    <div className="flex gap-1.5">
      <dt className="text-neutral-500">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </div>
  );
}
