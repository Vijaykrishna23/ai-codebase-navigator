# Embedding Options: Cost, Speed and Quality

Checked 2026-10-18. Prices per 1M tokens. Voyage and Jina figures are from their own pages; others are from third-party trackers, so verify before committing.

## 1. How much embedding this project does

- **Ingest** (once per repo): every code chunk gets embedded. A typical mid-sized repo is about 200k lines of Python/JS/TS, roughly **3M tokens**. A repo at the Medium limit (5,000 files) can reach about **10M tokens**.
- **Ask** (every question): only the question and a few search phrases, a few hundred tokens. Cost and speed here are negligible for every option.

So the choice is really about ingest cost, ingest speed, and retrieval quality on code.

## 2. Comparison

| Model | Type | Price | Free allowance | Cost per typical repo (3M tok) | Code quality (MTEB Code) | License / notes |
|---|---|---|---|---|---|---|
| **Voyage `voyage-code-4`** | API | $0.12 | **200M tokens** | $0.36 (free for ~60 repos) | Not published; successor to voyage-code-3 (79.8, best in the table) | Closed. 32k context, 1024 dims |
| Voyage `voyage-code-3` | API | $0.18 | unclear | $0.54 | **79.8** | Older generation |
| Voyage `voyage-4-lite` | API | $0.02 | 200M tokens | $0.06 | Not published | General purpose, not code-tuned |
| OpenAI `text-embedding-3-small` | API | $0.02 | No | $0.06 | Not published | General purpose, cheapest paid option |
| OpenAI `text-embedding-3-large` | API | $0.13 | No | $0.39 | 65.2 on CoIR (vendor-reported by a competitor) | General purpose |
| Gemini Embedding 2 | API | $0.20 | Yes, rate limited | $0.60 or free | Not published | Free-tier data used to improve Google products |
| **`jina-code-embeddings-0.5b`** | Local | Free | n/a | $0 | **78.7** | **CC BY-NC 4.0: non-commercial only**. 0.5B params, 896 dims |
| `jina-code-embeddings-1.5b` | Local | Free | n/a | $0 | 78.9 | CC BY-NC 4.0. Slower, small gain over 0.5b |
| **`Qwen3-Embedding-0.6B`** | Local | Free | n/a | $0 | **74.7** | Apache 2.0, commercial OK. 0.6B params |
| `jina-embeddings-v2-base-code` | Local | Free | n/a | $0 | Not published (2024 model, older) | Apache 2.0. 161M params, fastest local option |

Quality numbers are from Jina's own benchmark report (MTEB Code average), so treat the ranking as directional. The earlier draft of the design picked `jina-embeddings-v2-base-code`; newer models beat it, so it is no longer the recommendation.

## 3. Speed

These are rough estimates, not measurements, and should be benchmarked on your Mac.

| Option | Ingest a 3M-token repo | Per question | Hosting footprint |
|---|---|---|---|
| API (Voyage, OpenAI, Gemini) | ~1 to 3 min, limited by rate limits and batching | ~100 to 300 ms network | None |
| Local 0.5B model on an Apple Silicon Mac (GPU) | ~10 to 25 min | < 100 ms | n/a |
| Local 0.5B model on a small cloud CPU server | ~30 to 90 min | ~100 to 300 ms | ~2 GB RAM, plus CPU contention with the API |
| Local 161M model (`jina-v2-base-code`) | about 3x faster than 0.5B | < 50 ms | ~0.7 GB RAM |

## 4. Tradeoffs

- **API, code-tuned (Voyage code)**: best quality, fastest ingest, nothing to host, and the 200M free tokens cover all of development, the eval, and early hosting. Downsides: code is sent to Voyage (fine, the MVP only handles public repos) and you need an account.
- **API, cheap general-purpose (OpenAI small, Voyage lite)**: almost free, but not tuned for code, so expect weaker semantic search.
- **Local, Jina code 0.5b**: near-Voyage quality at $0, but the non-commercial license blocks any paid product later, and it makes hosting heavier.
- **Local, Qwen3-Embedding-0.6B**: free, commercial-friendly license, about 4 to 5 points behind on code retrieval, same hosting weight as Jina.
- **Gemini Embedding 2**: free tier, but no code-retrieval numbers found and rate limits slow ingest.

Semantic search is one of three retrieval paths (text and symbol search are the others), so embedding quality matters, but less than in plain RAG.

## 5. Recommendation

**`voyage-code-4` via API.** It has the best expected code-retrieval quality, ingests in minutes, adds nothing to hosting (which removes the concern about running a model on the server), and the 200M free tokens mean it is effectively free for this MVP: about 60 typical repos before any charge, then about $0.36 per repo.

**Fallback if you want no external account: `Qwen3-Embedding-0.6B` locally** (free, commercial-friendly). Use `jina-code-embeddings-0.5b` instead only if the project stays non-commercial.

Embeddings sit behind one interface, and the vector column size is set per model, so switching later only requires a re-index.

## Sources

- [Voyage AI pricing (official)](https://docs.voyageai.com/docs/pricing)
- [Jina Code Embeddings: SOTA code retrieval at 0.5B and 1.5B (Jina)](https://jina.ai/news/jina-code-embeddings-sota-code-retrieval-at-0-5b-and-1-5b)
- [jina-code-embeddings-0.5b model card (Hugging Face)](https://huggingface.co/jinaai/jina-code-embeddings-0.5b)
- [jina-embeddings-v2-base-code model card (Hugging Face)](https://huggingface.co/jinaai/jina-embeddings-v2-base-code)
- [Best Embedding Models in 2026 (OpenRouter)](https://openrouter.ai/blog/insights/best-embedding-models-2026/)
- [Qodo-Embed-1 code retrieval (Qodo)](https://www.qodo.ai/blog/qodo-embed-1-code-embedding-code-retrieval/)
- [OpenAI Embedding Pricing 2026 (embeddingcost.com)](https://embeddingcost.com/openai)
- [Best Embedding Models 2026: 15 Ranked and Priced (theairankings)](https://theairankings.com/best-embedding-models/)
- [Gemini Developer API pricing (Google)](https://ai.google.dev/gemini-api/docs/pricing)
