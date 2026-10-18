# AI Codebase Navigator: MVP Design

Status: draft v1, based on [problem-statement.md](problem-statement.md).

## 1. Summary

The Navigator takes a public GitHub repo URL and a natural-language question, investigates the code with a set of search and code-intelligence tools, and returns an evidence-backed answer with two layers:

1. **High-level explanation**: what happens and how the pieces fit.
2. **Code-level evidence**: files, symbols, line ranges, a call flow, related tests, and GitHub links pinned to the indexed commit.

The core design choice is to treat answering as an **investigation loop**: an LLM agent decides which tools to call (search, find definition, find callers, read source, find tests) until it has enough evidence, instead of a single "embed question, fetch top-k chunks, answer" pass. That is exactly the hypothesis the project tests, so the plain RAG path is also built, as a baseline mode, and both are run on the same questions.

## 2. Scope

**In scope (MVP)**
- Public GitHub repos only.
- Python, JavaScript, TypeScript (incl. JSX/TSX).
- Exact text search, symbol search, semantic search.
- Code intelligence: definitions, references, callers, callees, imports, importers, related tests.
- Natural-language investigation with streamed progress.
- Answers that separate repository facts from AI interpretation, and say so when evidence is missing.
- Baseline (RAG only) vs. Navigator evaluation harness.

**Out of scope (MVP)**, from the problem statement: git history and blame, issues/PRs, code generation or modification, PR creation, bug fixing, full architecture visualization, more languages, private repos, IDE extensions, real-time sync.

## 3. User flow

1. Paste a GitHub URL. The backend clones it at the default branch head, records the commit SHA, and indexes it. The UI shows indexing stages.
2. Ask a question in a chat view for that repo.
3. While the agent works, the UI streams investigation steps ("Searching for token validation", "Finding callers of `validate_token`").
4. The final answer renders as:
   - High-level explanation, with each sentence tagged as **fact** (backed by citations) or **interpretation**.
   - Code flow tree (when the question is about a flow).
   - Code references: path, symbol, line range, link to `github.com/{owner}/{repo}/blob/{sha}/{path}#L{start}-L{end}`.
   - Related tests.
5. Follow-up questions reuse the conversation and the evidence already gathered.

A file viewer is not required for the MVP since references link to GitHub. An inline snippet preview per reference is included.

## 4. Architecture

```
Next.js (App Router, TS)
   |  REST + SSE
   v
FastAPI (Python 3.13, uv)
   |
   |-- Ingestion:  clone -> walk -> parse (tree-sitter) -> symbol graph -> chunk -> embed
   |-- Tools:      text_search, symbol_search, semantic_search, get_definition,
   |               find_references, get_callers, get_callees, get_imports,
   |               get_importers, find_tests, read_code, list_dir, repo_overview
   |-- Agents:     Navigator (tool loop)  |  Baseline (RAG only)
   |-- Answer:     structured answer + citation validation
   |
   +-- Postgres + pgvector (repos, files, symbols, edges, chunks, embeddings, conversations)
   +-- Data dir (cloned repos), ripgrep for text search
   +-- Claude API (reasoning), embedding API (semantic search)
```

### Why these choices
- **Postgres + pgvector**: one store for relational data, the symbol graph and vectors. Runs locally as a Homebrew Postgres 17 install and maps directly to managed Postgres when hosted. SQLAlchemy 2 + Alembic for models and migrations.
- **ripgrep** for exact text search: fast, regex-capable, respects `.gitignore`, no index to maintain.
- **tree-sitter** (via `tree-sitter-language-pack`) for parsing: one approach for all three languages, gives symbols with exact line ranges.
- **Claude** as the reasoning model with native tool use. Model ID kept in config.

## 5. Ingestion

1. **Validate** the URL is a public `github.com/{owner}/{repo}` repo.
2. **Clone** shallow (`--depth 1`) into `data/repos/{owner}__{repo}__{sha}`. Record the SHA so all links are stable. Enforce size, file-count and time limits.
3. **Walk** files: skip binaries, vendored and generated dirs (`node_modules`, `dist`, `build`, `.venv`, `__pycache__`), minified files, lockfiles, files over a size cap.
4. **Parse** `.py`, `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` with tree-sitter. Extract:
   - Symbols: module, class, function, method, with qualified name, parent, start/end line, signature, docstring.
   - Imports: what each file imports and under which local names.
   - Call sites: callee expression text and line, attributed to the enclosing symbol.
5. **Resolve** edges (section 6) into `imports`, `calls`, `inherits`, `tests` edges.
6. **Chunk** by symbol (function or class body; long ones split with overlap), plus a file-header chunk with imports and module docstring. Each chunk keeps path and line range.
7. **Embed** chunks in batches.
8. **Summarize** (cheap, optional for v1): short per-file summaries plus a repo overview built from README, top-level layout and entry points. Used by "what does this repo do" questions.

Index status: `queued -> cloning -> parsing -> embedding -> ready | failed`.

## 6. Code intelligence

Static resolution with tree-sitter is heuristic, so every edge carries a **confidence**:

- **Imports**: resolve Python module paths and JS/TS relative paths and `tsconfig` path aliases to files in the repo. External packages are recorded but not followed.
- **Calls**: for each call site, try in order: a local or imported name bound in the same file (high), `self.method` / `this.method` within the class (high), a unique symbol name across the repo (medium), several candidates (low, all kept). Dynamic dispatch and callbacks are not resolved.
- **References**: resolved call and import edges, plus ripgrep identifier matches as low-confidence fallback.
- **Tests**: files matching test conventions (`test_*.py`, `*_test.py`, `*.test.ts`, `*.spec.js`, `tests/`, `__tests__/`) linked to code via imports and calls from test functions, plus name matching.

The agent sees confidence values and is instructed to state uncertainty when it relies on medium or low edges. If heuristic resolution proves too weak in evaluation, the upgrade path is SCIP indexers (`scip-python`, `scip-typescript`) behind the same tool interfaces.

## 7. Investigation agent

A tool-use loop with Claude:

- **System prompt** explains the tools, the two-layer answer contract, and the fact vs. interpretation rule.
- **Tools** (all return compact JSON with `path`, `symbol`, `start_line`, `end_line`, and an `evidence_id`):

| Tool | Purpose |
|---|---|
| `repo_overview()` | README summary, languages, top-level layout, entry points |
| `list_dir(path)` | Directory contents |
| `text_search(pattern, path_glob?)` | ripgrep exact or regex search |
| `symbol_search(name)` | Find symbols by name or qualified name |
| `semantic_search(query, k)` | Embedding search over chunks |
| `get_definition(symbol)` | Location and source of a symbol |
| `find_references(symbol)` | All usages, grouped by file |
| `get_callers(symbol)` / `get_callees(symbol)` | Call graph neighbours with confidence |
| `get_imports(path)` / `get_importers(path)` | Module dependency edges |
| `find_tests(symbol_or_path)` | Tests that exercise the target |
| `read_code(path, start, end)` | Read a source range |

- **Strategy** is left to the model, with hints for common question categories from the problem statement (Understanding, Location, Relationships, Flow, Testing). For example, a "where is X called" question should go symbol_search then find_references and stop; a "how does X work" question should search, pick entry points, follow callees, read key code and find tests.
- **Budget**: cap tool calls and tokens per question; when the cap is hit the agent must answer with what it has and flag gaps.
- **Evidence ledger**: every tool result is stored with its `evidence_id`. The final answer may only cite ids from the ledger.

## 8. Answer contract

The agent finishes by calling a `submit_answer` tool with a structured payload:

```json
{
  "summary": "one or two sentences",
  "explanation": [
    {"text": "AuthMiddleware.authenticate() calls validate_token().", "kind": "fact", "evidence": ["e12"]},
    {"text": "This keeps token details out of the middleware.", "kind": "interpretation", "evidence": ["e12", "e15"]}
  ],
  "flow": {"symbol": "AuthMiddleware.authenticate", "evidence": "e12", "children": [ ... ]},
  "references": [{"path": "auth/token.py", "symbol": "validate_token", "start_line": 18, "end_line": 43, "evidence": "e15"}],
  "tests": [{"path": "tests/auth/test_token.py", "symbol": "test_expired_token", "start_line": 20, "end_line": 35, "evidence": "e20"}],
  "gaps": ["Could not resolve where get_user is configured; it is injected at runtime."]
}
```

The backend **validates** it before sending: every cited evidence id exists, every path exists at the indexed SHA, line ranges are inside the file. A `fact` with no valid evidence is downgraded to `interpretation` or dropped. GitHub links are built server-side, never by the model.

## 9. Baseline mode

Same UI and answer rendering, but the pipeline is: embed the question, take top-k chunks from `semantic_search`, one LLM call with those chunks. It is selectable per question (a toggle in the UI and a parameter in the API) so both systems can be compared side by side.

## 10. Evaluation

`eval/` holds a small benchmark:
- 3 to 5 real public repos across Python, JS and TS.
- 10 to 20 questions per repo covering all five categories, each with gold references (files and symbols a correct answer must mention).
- A runner that asks both modes and records answers, tool traces, latency and token cost.
- Metrics: reference precision and recall against gold, citation validity rate, and a rubric score (correctness, completeness, fact vs. interpretation honesty) from an LLM judge, spot-checked by hand.

## 11. API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/repos` | Body `{url}`. Clone and index, returns repo id |
| GET | `/api/repos` | List indexed repos |
| GET | `/api/repos/{id}` | Status, SHA, languages, counts |
| DELETE | `/api/repos/{id}` | Remove a repo and its index |
| POST | `/api/repos/{id}/conversations` | Start a conversation |
| POST | `/api/conversations/{cid}/messages` | Body `{question, mode: "navigator" or "baseline"}`. SSE stream of `step`, `answer`, `error` events |
| GET | `/api/conversations/{cid}` | History with answers |

## 12. Data model (Postgres)

- `repos`: id, owner, name, url, sha, status, error, stats, created_at
- `files`: id, repo_id, path, language, loc, is_test, summary
- `symbols`: id, file_id, kind, name, qualified_name, parent_id, start_line, end_line, signature, docstring
- `edges`: id, repo_id, kind (`imports`, `calls`, `inherits`, `tests`), src_symbol_id or src_file_id, dst_symbol_id or dst_file_id, dst_name, line, confidence
- `chunks`: id, file_id, symbol_id, start_line, end_line, content; embedding `vector(N)` with N set by the embedding model (pgvector, HNSW index)
- `conversations`, `messages`: messages store mode, structured answer JSON, evidence ledger and tool trace

## 13. Frontend

- Next.js App Router, TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, Shiki for snippet highlighting.
- Two pages, matching the two-step flow:
  - **Ingest** (`/`): paste a GitHub URL, watch indexing progress, see the list of indexed repos and their status.
  - **Chat** (`/repos/[id]`): ask questions about a `ready` repo, with answers, trace and mode toggle.
- Answer components: explanation with fact and interpretation styling, collapsible flow tree, reference cards (snippet + GitHub link), tests list, gaps callout, and a collapsible "investigation trace" of the tool steps.
- Mode toggle: Navigator or Baseline.

## 14. Repository layout

```
ai-codebase-navigator/
  frontend/                 Next.js app
  backend/                  uv project
    pyproject.toml
    app/
      main.py
      api/                  routers
      core/                 config, db, logging
      ingest/               clone, walk, parse, resolve, chunk, embed
      intel/                symbol graph queries
      search/               text, symbol, semantic
      agent/                navigator loop, tools, prompts, baseline
      answer/               schema, validation, link building
  eval/                     questions, gold refs, runner
  docs/
    problem-statement.md
    design.md
  docker-compose.yml        Optional full stack in containers
  .env.example
```

## 15. Milestones

1. **Skeleton**: Next.js and FastAPI apps, uv project, health check end to end.
2. **Ingest**: clone, walk, tree-sitter symbols for Python, JS, TS, status tracking.
3. **Search**: text, symbol and semantic search tools, plus the baseline mode working end to end.
4. **Code intelligence**: imports, calls, references, tests with confidence.
5. **Navigator agent**: tool loop, evidence ledger, structured answer, validation, streamed steps.
6. **UI answer view**: explanation, flow, references, tests, trace.
7. **Evaluation**: benchmark repos and questions, runner, first comparison.

Hosting is decided after milestone 7.

## 16. Decisions

### Two-step flow
**Decided.** Two pages: Ingest and Chat (section 13).

Step 1 **Ingest**: `POST /api/repos` with a URL, then poll status until `ready`. Step 2 **Ask**: questions go only to repos that are `ready`. Ingestion is a one-time cost per repo and SHA; asking never re-clones.

### Embeddings
**Decided: `voyage-code-4` via API** (best expected code-retrieval quality, fast ingest, nothing to host, 200M free tokens). Fallback: `Qwen3-Embedding-0.6B` run locally. Full cost, speed and quality comparison in [embedding-options.md](embedding-options.md).

Embeddings sit behind one interface, so swapping later only needs a re-index.

### LLM
**Decided: Claude Haiku 5.5 for development.** Claude Sonnet 5.5 remains the proposed model for the Navigator vs. Baseline comparison runs, all behind a provider layer so the model is config. Full cost and quality comparison in [llm-options.md](llm-options.md).

### Repo size limits
**Decided: Medium.**

| Option | Limits | Fits |
|---|---|---|
| Small | 1,000 source files, 50 MB clone, 5 min ingest | Libraries and small apps |
| **Medium (recommended)** | 5,000 source files, 200 MB clone, 15 min ingest | Most real-world services and popular OSS libraries |
| Large | 20,000 source files, 1 GB clone, 60 min ingest | Big monorepos; local embeddings get slow here |

Limits apply to Python, JS and TS source files after skipping vendored and generated code, and are config values.

### Database
**Decided: Postgres + pgvector.** Local dev on Homebrew Postgres 17 + pgvector; `docker-compose.yml` runs the full stack in containers as an option; managed Postgres with the pgvector extension when hosted.
