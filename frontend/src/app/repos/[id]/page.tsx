import Link from "next/link";
import { Suspense } from "react";

import { ApiError, getRepo } from "@/lib/api";

async function RepoHeader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const repo = await getRepo(id).catch((e: unknown) => {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  });
  if (!repo) return <>Repository not found</>;
  return (
    <div className="flex flex-col">
      <span>
        {repo.owner}/{repo.name}
      </span>
      {repo.sha && (
        <span className="font-mono text-xs font-normal text-neutral-500">
          {repo.sha.slice(0, 7)}
          {repo.status !== "ready" && ` (${repo.status})`}
        </span>
      )}
    </div>
  );
}

export default function ChatPage(props: PageProps<"/repos/[id]">) {
  return (
    <main className="mx-auto flex h-full w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-12">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">
          <Suspense fallback="Repository">
            <RepoHeader params={props.params} />
          </Suspense>
        </h1>
        <Link href="/" className="text-sm text-neutral-500 hover:underline">
          Back to repositories
        </Link>
      </header>

      <section className="flex-1 rounded-md border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-700">
        Answers will appear here.
      </section>

      <form className="flex gap-2">
        <input
          name="question"
          placeholder="How does token validation work?"
          className="flex-1 rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
          disabled
        />
        <button
          type="submit"
          className="rounded-md bg-neutral-900 px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          disabled
        >
          Ask
        </button>
      </form>
    </main>
  );
}
