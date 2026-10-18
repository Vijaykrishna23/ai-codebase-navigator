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

export type RepoStatus = "queued" | "cloning" | "parsing" | "ready" | "failed";

export type RepoStats = {
  clone_mb?: number;
  files?: number;
  source_files?: number;
  test_files?: number;
  languages?: Record<string, number>;
  parsed?: number;
  symbols?: number;
  parse_errors?: number;
  duration_s?: number;
};

export type Repo = {
  id: number;
  owner: string;
  name: string;
  url: string;
  default_branch: string | null;
  sha: string | null;
  status: RepoStatus;
  error: string | null;
  stats: RepoStats;
  created_at: string;
  updated_at: string;
  indexed_at: string | null;
};

export const ACTIVE_STATUSES: RepoStatus[] = ["queued", "cloning", "parsing"];

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : res.statusText;
    throw new ApiError(detail, res.status);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export function getHealth(): Promise<Health> {
  return request("/api/health");
}

export function listRepos(): Promise<Repo[]> {
  return request("/api/repos");
}

export function getRepo(id: string | number): Promise<Repo> {
  return request(`/api/repos/${id}`);
}

export function createRepo(url: string): Promise<Repo> {
  return request("/api/repos", { method: "POST", body: JSON.stringify({ url }) });
}

export function reindexRepo(id: number): Promise<Repo> {
  return request(`/api/repos/${id}/reindex`, { method: "POST" });
}

export function deleteRepo(id: number): Promise<void> {
  return request(`/api/repos/${id}`, { method: "DELETE" });
}
