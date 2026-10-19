# First Navigator vs Baseline comparison (2026-10-19)

48 questions (12 each on click, express, ky, zustand), both modes, judged by Sonnet 5.5. Full tables and per-question judge notes are in `results/<run>/report.md`.

## Sonnet 5.5, Navigator effort medium (complete: 96 of 96 answers)

| Metric | Navigator | Baseline |
|---|---|---|
| File recall | 88% | 90% |
| Symbol recall | 90% | 95% |
| File precision | 86% | 65% |
| Judge correctness / completeness / honesty | 4.21 / 3.91 / 4.19 | 4.33 / 4.25 / 4.19 |
| Verdicts correct / partial / wrong | 34 / 13 / 0 | 40 / 8 / 0 |
| Avg tool calls | 3.0 | 0 |
| Median latency | 10.0 s | 9.8 s |
| Cost, whole run | $1.15 | $1.83 |

## Haiku 5.5, Navigator effort medium (incomplete: 39 of 48 per mode)

The API credit ran out during this run, so zustand-04 to zustand-12 failed in both modes.

| Metric | Navigator | Baseline |
|---|---|---|
| File recall | 96% | 88% |
| Symbol recall | 98% | 94% |
| Judge correctness / completeness / honesty | 4.36 / 4.36 / 4.00 | 4.29 / 4.03 / 4.26 |
| Verdicts correct / partial / wrong | 33 / 6 / 0 | 31 / 7 / 0 |
| Avg tool calls | 8.1 | 0 |
| Median latency | 14.7 s | 7.1 s |

## What it says

- On Haiku the Navigator wins on recall and completeness, as the design predicted. On Sonnet it does not yet: it is more precise (86% vs 65% of cited files on target) but less complete.
- The cause on Sonnet is under-investigation. Sonnet's Navigator made 1 or 2 tool calls on 26 of 48 questions, against Haiku's 8 on average, and answered from that. Its weakest categories are understanding (56% file recall) and testing (completeness 3.5). Likely fixes, to measure next: effort `high` for Sonnet (its effort levels are recalibrated and default to high), and a prompt rule to read the code and find tests before submitting for understanding, flow and testing questions.
- No answer was judged wrong in any run. Citation validity was 98 to 100%.
- The benchmark is easy for Baseline. These are small, well-documented repos, and top-12 semantic chunks already cover most questions. Harder multi-hop questions, or a larger repo, would separate the modes better.

## Follow-up: Sonnet Navigator at effort high (2026-10-19)

Same 16 understanding and testing questions, Navigator only, both runs judged by Haiku 5.5 (the medium answers were re-judged so the judge matches). Run: `results/20261019-060811_claude-sonnet-5-5`.

| Metric | Effort medium | Effort high |
|---|---|---|
| File recall | 72% | 88% |
| Symbol recall | 89% | 100% |
| Judge correctness / completeness / honesty | 3.50 / 3.06 / 3.62 | 3.69 / 3.88 / 3.81 |
| Verdicts correct / partial / wrong | 5 / 11 / 0 | 11 / 5 / 0 |
| Avg tool calls | 3.1 | 5.8 |
| Median latency | 10.7 s | 17.3 s |
| Cost per question | $0.025 | $0.043 |

Effort high fixes most of the under-investigation on Sonnet, at about 1.7x the cost per question. Haiku's judge scores lower than Sonnet's on the same answers, so compare judge scores only within one judge.

## Bugs found and fixed during the first Haiku pass

- A malformed `read_code` line number (`"596, "`) raised ValueError and failed the whole answer. Tool argument ValueErrors now go back to the model as a tool error (`backend/app/agent/tools.py`).
- Haiku sometimes put the whole answer into `summary` as tags (`</summary><explanation><item>...`), or filled only the summary. The milestone 5 repair step did not catch that form. 5 of 48 Navigator answers came back with no citations. The Navigator now rejects that submission and asks the model to resubmit (`_flattened` in `backend/app/agent/navigator.py`). After the fix, 0 of 87 Navigator answers across both runs had empty or leaked fields.
