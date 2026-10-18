export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Health = {
  status: "ok" | "degraded";
  database: string;
  pgvector: string | null;
  llm_model: string;
  llm_key_set: boolean;
  embedding_model: string;
  embedding_key_set: boolean;
};

export async function getHealth(): Promise<Health> {
  const res = await fetch(`${API_URL}/api/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
  return res.json();
}
