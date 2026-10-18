"use client";

import { useEffect, useState } from "react";

import { getHealth, type Health } from "@/lib/api";

export function BackendStatus() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return <p className="text-sm text-red-600">Backend unreachable: {error}</p>;
  }
  if (!health) {
    return <p className="text-sm text-neutral-500">Checking backend...</p>;
  }

  const rows: [string, string][] = [
    ["Database", health.database],
    ["pgvector", health.pgvector ?? "not installed"],
    ["LLM", `${health.llm_model} (${health.llm_key_set ? "key set" : "no key"})`],
    ["Embeddings", `${health.embedding_model} (${health.embedding_key_set ? "key set" : "no key"})`],
  ];

  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-neutral-500">{label}</dt>
          <dd className="font-mono">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
