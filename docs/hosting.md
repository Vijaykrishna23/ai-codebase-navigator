# Hosting Plan

Status: proposal, nothing deployed and no accounts created. Pick an option in section 5 before anything is set up.

Prices were checked on 2026-10-19 against vendor pages and recent third-party write-ups. Several vendors raised prices this year (Hetzner in April and June, Fly.io in October), so confirm the number on the vendor's own pricing page at signup.

## 1. What the app needs from a host

| Need | Why it matters | Where it comes from |
| --- | --- | --- |
| A long-running backend process | Ingest runs as an in-process `asyncio` task and can take up to `ingest_timeout_s` (900 s). A host that sleeps or scales to zero mid-job kills the ingest. | `backend/app/ingest/pipeline.py:61` |
| One uvicorn worker | Job state and the `max_concurrent_ingests` limit live in process memory. More workers would each keep their own copy. | `backend/app/core/config.py:37` |
| Persistent disk for clones | Cloned repos live under `DATA_DIR/repos`, up to `max_clone_mb` (200 MB) each. `rg` reads them at question time, so they must survive restarts. | `backend/app/ingest/pipeline.py:39` |
| `git` and `ripgrep` binaries | Cloning and exact text search shell out to them. The backend Dockerfile already installs both. | `backend/Dockerfile:8` |
| Postgres 17 with pgvector | Chunks, symbols, edges and 1024-dim embeddings. Today's local DB is 84 MB for 4 small repos (about 4,100 chunks), so roughly 20 KB per chunk including indexes. 1 GB holds on the order of 50,000 chunks, or 10 to 20 small and medium repos. | local `pg_database_size` |
| Long-lived HTTP responses | Answers stream over SSE while the agent runs, which can take a minute or more. Proxies with short idle timeouts cut them off. | design.md |
| Secrets | `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `DATABASE_URL`. | `.env.example` |
| Next.js frontend | Built with `output: "standalone"`. `NEXT_PUBLIC_API_URL` is baked in at build time, not read at runtime. | `frontend/next.config.ts`, `frontend/Dockerfile` |

Everything the backend serves is under `/api`, so one domain can serve both: `/api/*` to FastAPI, everything else to Next.js. That removes CORS from the picture.

## 2. Options

### A. One small VPS running the existing docker-compose (Hetzner)

One Hetzner Cloud server runs `docker-compose.yml` as it is, plus Caddy in front for HTTPS and the `/api` split. Docker runs on the server, not on the Mac.

- **Cost:** about €5 to €6 a month (roughly $6) for 2 vCPU, 4 GB RAM, 40 GB disk. Pick an x86 `CX` plan over the ARM `CAX` one so the Python wheels (tree-sitter, asyncpg) match what is already tested. Add a domain if you want one (about $10 a year), or use the server IP with a free `sslip.io` hostname.
- **Fits the app:** fully. Always on, local disk for clones, Postgres on the same box, no proxy timeouts, compose file already written.
- **Downsides:** you own the server. OS updates, firewall, and backups are on you. A nightly `pg_dump` to the server's disk plus Hetzner snapshots (20% of server price, about €1) covers backups.

### B. Vercel (frontend) + Fly.io (backend) + Neon (Postgres)

- **Cost:** about $7 a month.
  - Vercel Hobby: $0. Its terms limit Hobby to non-commercial use.
  - Fly.io `shared-cpu-1x` with 1 GB RAM: about $6.70 a month after the October 2026 change, plus a 3 GB volume at $0.15/GB ($0.45). Must set `min_machines_running = 1` and turn off auto-stop, or ingests die.
  - Neon Free: $0, 1 GB storage per project since 2026-10-02, pgvector supported. It suspends when idle, so the first query after a quiet spell takes a second or two.
- **Fits the app:** yes, with care. Fly's proxy allows long SSE streams. The 1 GB Neon cap is the real ceiling, see section 1.
- **Downsides:** three accounts and three dashboards. Two origins, so `CORS_ORIGINS` and `NEXT_PUBLIC_API_URL` must point at each other. Hitting Neon's 1 GB means moving to its paid plan (usage based, starts around $5 to $19 a month depending on usage).

### C. Railway (everything in one project)

- **Cost:** $5 a month Hobby plan, which includes $5 of usage. Backend (about 0.5 GB RAM idle), Postgres with the pgvector template, and a small volume likely land at $5 to $10 a month. Frontend can go to Vercel free to keep usage down.
- **Fits the app:** yes. Always-on services, volumes, Dockerfile deploys from the repo.
- **Downsides:** usage billing makes the monthly number less predictable than a flat VPS. Deploying from GitHub needs the repo connected, which is fine since nothing has to be pushed by Claude.

### D. Render

- **Cost:** about $15 or more a month. The free web service sleeps after 15 minutes and has no persistent disk, so it cannot host the backend. The cheapest always-on service is $7, a persistent disk adds about $0.25/GB, and Postgres starts around $6 to $7 (the free Postgres is time-limited).
- **Verdict:** costs twice option A for the same result. Not recommended.

### E. Oracle Cloud Always Free

- **Cost:** $0 for an ARM VM with up to 4 cores and 24 GB RAM.
- **Verdict:** tempting on paper, but signup often fails on card checks, ARM capacity in popular regions is frequently unavailable, and Oracle reclaims idle free instances. Worth trying only if $0 matters more than reliability.

### Not considered

Supabase Free for the database: 500 MB storage and projects pause after 7 days of inactivity, so a demo link would go dead between uses. AWS, GCP, Azure: no tier that is both always-on and cheaper than option A once disk and a database are added.

## 3. Comparison

| | Monthly cost | Accounts | Ingest safe | Repo capacity | Ops on you |
| --- | --- | --- | --- | --- | --- |
| **A. Hetzner VPS** | ~$6 | 1 | Yes | ~35 GB free disk, many repos | Updates, backups |
| B. Vercel + Fly + Neon | ~$7 | 3 | Yes, if auto-stop off | ~1 GB DB, 10 to 20 repos | Little |
| C. Railway | $5 to $10 | 1 (+Vercel) | Yes | Grows with spend | Little |
| D. Render | $15+ | 1 | Paid tier only | Grows with spend | Little |
| E. Oracle Free | $0 | 1 | Yes | Large | Updates, backups, capacity hunting |

## 4. Recommendation

**Option A, a Hetzner x86 VPS with docker-compose and Caddy.** It is the cheapest reliable choice, the price is flat, there are no storage caps that bite at 10 repos, and it reuses `docker-compose.yml` with small changes. Option B is the pick if you would rather not maintain a server at all, at about the same cost but with a 1 GB database ceiling.

## 5. Changes needed before any public deploy

These apply to every option.

1. **Access gate (required).** The app has no authentication. Anyone with the URL could index repos and ask questions on your Anthropic and Voyage keys. Simplest fix: HTTP basic auth in Caddy (option A) or a shared access token checked by a small FastAPI dependency (options B to D). Not optional while you pay personally.
2. **Provider spend limits.** Set a monthly cap in the Anthropic console and the Voyage dashboard, so a bug or a leaked link has a hard ceiling.
3. **Production env.** `CORS_ORIGINS` set to the real origin (or unused with the same-domain setup), `NEXT_PUBLIC_API_URL` passed as a build arg with the public URL, a non-default Postgres password, and `DATA_DIR` on the persistent volume.
4. **Keep one worker.** The Dockerfile CMD already runs a single uvicorn process. Leave it that way.
5. **Do not publish Postgres.** Drop the `5433:5432` port mapping in production so the database is reachable only from the backend.
6. **Backups.** Nightly `pg_dump`. Clones can always be re-fetched, so only the database needs backing up.

For option A that means a `docker-compose.prod.yml` override (Caddy service, no DB port, real env) and a `Caddyfile`. That is a few small files and no code change except the access gate, if done in the app instead of Caddy.

## 6. Steps once an option is chosen (option A)

1. You create the Hetzner account and server (Ubuntu 24.04, x86 CX plan, your SSH key) and set the Anthropic and Voyage spend caps.
2. Claude writes `docker-compose.prod.yml`, `Caddyfile`, and a short deploy section in the README.
3. On the server: install Docker, clone the repo, create `.env` with the keys and a strong DB password, run `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
4. Index one small repo (for example `ky`) and ask one question to confirm SSE streaming and links work end to end. That costs a few cents of API usage.

## Sources

- [Hetzner pricing changes, April 2026](https://agentdeals.dev/hetzner-pricing-2026), [June 2026 repricing](https://privatedevops.com/news/hetzner-june-2026-cloud-price-increase-what-to-do), [Hetzner Cloud What's New](https://docs.hetzner.cloud/whats-new)
- [Fly.io resource pricing](https://fly.io/docs/about/pricing/), [Fly.io pricing 2026](https://temps.sh/blog/fly-io-pricing-2026)
- [Neon changelog 2026-10-02](https://neon.com/docs/changelog/2026-10-02), [Neon pricing](https://neon.com/pricing)
- [Railway vs Render vs Fly pricing 2026](https://sota.io/blog/railway-vs-render-vs-fly-pricing-2026), [Render pricing 2026](https://temps.sh/blog/render-pricing-2026)
- [Supabase free tier limits 2026](https://automationatlas.io/answers/supabase-free-tier-limits-2026/)
