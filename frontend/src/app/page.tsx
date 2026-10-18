import { BackendStatus } from "@/components/BackendStatus";

export default function IngestPage() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-12">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">AI Codebase Navigator</h1>
        <p className="text-neutral-500">
          Step 1: add a public GitHub repository to index. Step 2: ask questions about it.
        </p>
      </header>

      <form className="flex gap-2">
        <input
          type="url"
          name="url"
          placeholder="https://github.com/owner/repo"
          className="flex-1 rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
          disabled
        />
        <button
          type="submit"
          className="rounded-md bg-neutral-900 px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-neutral-900"
          disabled
        >
          Ingest
        </button>
      </form>

      <section className="flex flex-col gap-3">
        <h2 className="font-medium">Indexed repositories</h2>
        <p className="text-sm text-neutral-500">None yet. Ingestion arrives in milestone 2.</p>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="font-medium">Backend status</h2>
        <BackendStatus />
      </section>
    </main>
  );
}
