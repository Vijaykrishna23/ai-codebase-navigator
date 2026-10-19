# Evaluation

Benchmark for the Navigator vs Baseline comparison (design section 10).

- `questions/*.json`: one file per repo. Each question has a category (`understanding`, `location`, `relationships`, `flow`, `testing`), gold `files` and `symbols` a correct answer must point to, `related_files` that are fine to cite, and an `expected` reference answer for the judge.
- `run.py`: asks every question in both modes, scores and judges the answers, and writes `results/<run>/answers.jsonl`, `summary.json` and `report.md`.
- `score.py`: metrics, the LLM judge and the report.

Repos, all indexed at the SHAs in `data/repos/`: pallets/click (Python), expressjs/express (JavaScript), sindresorhus/ky (TypeScript), pmndrs/zustand (TypeScript/TSX). 12 questions each.

## Run

The runner imports the backend and talks to Postgres directly, so the dev server does not need to be running. The repos must be ingested and `ready`.

```bash
cd backend && uv run python ../eval/run.py --model claude-sonnet-5-5
```

Useful flags: `--repos click ky`, `--ids click-01 ky-03`, `--modes navigator`, `--effort high`, `--no-judge`, `--judge-model claude-opus-5-5`, `--concurrency 3`. `--rescore results/<run>` recomputes metrics and re-judges saved answers without asking again, for after editing gold references.

## Metrics

- **File recall / symbol recall**: share of gold files and symbols the answer points to. A file counts when it appears in a reference, test, flow step or evidence cited by a sentence. A symbol counts when a cited symbol contains it or the answer text names it.
- **File precision**: share of cited files that are gold or related.
- **Citation validity**: evidence ids the model cited that exist in the ledger, before validation drops the rest.
- **Judge scores** (1 to 5): correctness, completeness and fact vs interpretation honesty, plus a correct / partial / wrong verdict, from Haiku 5.5 by default (`--judge-model claude-sonnet-5-5` for a stricter, about 20x pricier judge). Spot-check the per-question notes in `report.md` by hand.
- Also: facts downgraded for lack of a valid citation, summaries with leaked answer fields, latency, tool calls and estimated cost. Cost uses the 5-minute cache-write price, so Navigator cost is slightly underestimated.
