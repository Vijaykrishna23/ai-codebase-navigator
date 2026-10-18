import { BackendStatus } from "@/components/BackendStatus";
import { RepoIngest } from "@/components/RepoIngest";

export default function IngestPage() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-12">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">AI Codebase Navigator</h1>
        <p className="text-neutral-500">
          Step 1: add a public GitHub repository to index. Step 2: ask questions about it.
        </p>
      </header>

      <RepoIngest />

      <section className="flex flex-col gap-3">
        <h2 className="font-medium">Backend status</h2>
        <BackendStatus />
      </section>
    </main>
  );
}
