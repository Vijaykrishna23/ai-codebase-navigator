# AI Codebase Navigator

Investigates unfamiliar public GitHub repositories and answers natural-language questions by connecting high-level explanations to the exact code, symbols, relationships and tests involved.

- Problem statement: [docs/problem-statement.md](docs/problem-statement.md)
- Design: [docs/design.md](docs/design.md)
- Model choices: [docs/llm-options.md](docs/llm-options.md), [docs/embedding-options.md](docs/embedding-options.md)

## Stack

- `frontend/`: Next.js (App Router, TypeScript, Tailwind)
- `backend/`: FastAPI, managed with uv, SQLAlchemy + Alembic
- Postgres 17 + pgvector, installed locally with Homebrew
- Claude (Haiku 5.5 for development) for reasoning, Voyage `voyage-code-4` for embeddings

## Prerequisites

- Homebrew
- uv
- Node.js 20+

## Setup

Install Postgres and pgvector, then start Postgres:

```bash
brew install postgresql@17 pgvector
```

```bash
LC_ALL=en_US.UTF-8 /opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /opt/homebrew/var/postgresql@17 -l /opt/homebrew/var/log/postgresql@17.log start
```

(Or `brew services start postgresql@17` to start it automatically at login.)

Create the database:

```bash
/opt/homebrew/opt/postgresql@17/bin/psql -d postgres -c "CREATE ROLE navigator LOGIN PASSWORD 'navigator';" -c "CREATE DATABASE navigator OWNER navigator;"
```

Configure and install:

```bash
cp .env.example .env
echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > frontend/.env.local
(cd backend && uv sync && uv run alembic upgrade head)
(cd frontend && npm install)
```

Add `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY` to `.env` when you have them. The health check works without them.

## Run

Backend on http://localhost:8000 (API docs at `/docs`):

```bash
cd backend && uv run uvicorn app.main:app --reload
```

Frontend on http://localhost:3000:

```bash
cd frontend && npm run dev
```

The Ingest page shows a backend status panel that confirms the API, database and pgvector are reachable.

## Run with Docker (optional)

`docker-compose.yml` runs the full stack (Postgres + pgvector, backend, frontend) in containers. Native setup above is the default for development; use this when you want an isolated environment or to test the hosting setup.

```bash
docker compose up --build
```

- Frontend: http://localhost:3000, backend: http://localhost:8000
- The Docker database is exposed on port 5433 so it does not clash with a native Postgres on 5432.
- API keys are read from `.env` at the repo root.
- The backend runs migrations on startup.
