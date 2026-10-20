# Backend

FastAPI service for AI Codebase Navigator. See the root README for setup.

## Logging

The app logs to the console under the `app.*` loggers, next to uvicorn's own
access log. Set `LOG_LEVEL` in the root `.env` (default `INFO`; `DEBUG` adds
per-batch ingest progress and each SSE event). Log lines carry a tag such as
`[repo 3]` or `[msg 12]` so concurrent ingests and questions can be told apart.
