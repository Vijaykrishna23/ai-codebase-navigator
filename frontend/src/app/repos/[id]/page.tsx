import Link from "next/link";
import { Suspense } from "react";

import { RepoWorkspace } from "@/components/RepoWorkspace";
import { ApiError, getRepo } from "@/lib/api";

async function loadRepo(params: Promise<{ id: string }>) {
  const { id } = await params;
  return getRepo(id).catch((e: unknown) => {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  });
}

async function RepoHeader({ params }: { params: Promise<{ id: string }> }) {
  const repo = await loadRepo(params);
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

async function Workspace({ params, searchParams }: PageProps<"/repos/[id]">) {
  const [repo, { c }] = await Promise.all([loadRepo(params), searchParams]);
  if (!repo) return null;
  const conversation = typeof c === "string" && /^\d+$/.test(c) ? Number(c) : null;
  return <RepoWorkspace key={repo.id} repo={repo} initialConversation={conversation} />;
}

export default function ChatPage(props: PageProps<"/repos/[id]">) {
  return (
    <main className="mx-auto flex h-full w-full max-w-6xl flex-1 flex-col gap-6 px-4 py-12">
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

      <Suspense fallback={<p className="text-sm text-neutral-500">Loading...</p>}>
        <Workspace {...props} />
      </Suspense>
    </main>
  );
}
