export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Health = {
  status: "ok" | "degraded";
  database: string;
  pgvector: string | null;
  llm_model: string;
  llm_key_set: boolean;
  embedding_model: string;
  embedding_key_set: boolean;
  ripgrep: boolean;
};

export type RepoStatus = "queued" | "cloning" | "parsing" | "embedding" | "ready" | "failed";

export type RepoStats = {
  clone_mb?: number;
  files?: number;
  source_files?: number;
  test_files?: number;
  languages?: Record<string, number>;
  parsed?: number;
  symbols?: number;
  parse_errors?: number;
  chunks?: number;
  embedded?: number;
  embedding?: "skipped" | "running" | "done" | "failed";
  embedding_note?: string | null;
  embedding_model?: string;
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

export const ACTIVE_STATUSES: RepoStatus[] = ["queued", "cloning", "parsing", "embedding"];

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

export function embedRepo(id: number): Promise<Repo> {
  return request(`/api/repos/${id}/embed`, { method: "POST" });
}

export type TextMatch = {
  path: string;
  line: number;
  text: string;
  ranges: [number, number][];
  url: string;
};

export type TextSearchResult = { matches: TextMatch[]; truncated: boolean };

export type SymbolHit = {
  id: number;
  kind: string;
  name: string;
  qualified_name: string;
  path: string;
  start_line: number;
  end_line: number;
  signature: string | null;
  docstring: string | null;
  is_test: boolean;
  url: string;
};

export type ChunkHit = {
  id: number;
  kind: string;
  path: string;
  symbol: string | null;
  start_line: number;
  end_line: number;
  content: string;
  score: number;
  is_test: boolean;
  url: string;
};

export type TextSearchOptions = {
  regex?: boolean;
  case?: "smart" | "sensitive" | "insensitive";
  glob?: string;
};

function query(params: Record<string, string | number | boolean | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  return q.toString();
}

export function searchText(
  repoId: number,
  q: string,
  opts: TextSearchOptions = {},
): Promise<TextSearchResult> {
  return request(`/api/repos/${repoId}/search/text?${query({ q, ...opts })}`);
}

export function searchSymbols(repoId: number, q: string, kind?: string): Promise<SymbolHit[]> {
  return request(`/api/repos/${repoId}/search/symbols?${query({ q, kind })}`);
}

export function searchSemantic(repoId: number, q: string, k = 10): Promise<ChunkHit[]> {
  return request(`/api/repos/${repoId}/search/semantic?${query({ q, k })}`);
}

export type AskMode = "baseline" | "navigator";

export type Evidence = {
  id: string;
  path: string;
  symbol: string | null;
  start_line: number;
  end_line: number;
  url: string;
  content: string;
  score: number | null;
  is_test: boolean;
};

export type AnswerSentence = {
  text: string;
  kind: "fact" | "interpretation";
  evidence: string[];
  downgraded: boolean;
};

export type AnswerReference = {
  path: string;
  symbol: string | null;
  start_line: number;
  end_line: number;
  url: string;
  evidence: string;
  note: string;
  is_test: boolean;
};

export type Answer = {
  mode: AskMode;
  question: string;
  summary: string;
  explanation: AnswerSentence[];
  references: AnswerReference[];
  tests: AnswerReference[];
  gaps: string[];
  evidence: Evidence[];
  usage: { model: string; input_tokens: number; output_tokens: number; latency_ms: number };
};

export function ask(repoId: number, question: string, mode: AskMode): Promise<Answer> {
  return request(`/api/repos/${repoId}/ask`, {
    method: "POST",
    body: JSON.stringify({ question, mode }),
  });
}
