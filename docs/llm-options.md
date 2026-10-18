# LLM Options: Cost and Tradeoffs

Prices checked 2026-10-18, USD per 1M tokens. Claude prices are from Anthropic's model table; Gemini from Google's official pricing page; OpenAI and DeepSeek from third-party trackers (OpenAI's own page could not be fetched), so verify those before committing. Full disclosure: this comparison was written by Claude.

## 1. What a question costs in this project

The Navigator is an agent loop, so one question is several model calls that each resend the growing conversation. Working estimate per Navigator question:

- About 10 tool-call turns.
- About 200k input tokens total across turns, of which roughly 150k are repeated prefix (cacheable) and 50k are new.
- About 10k output tokens (reasoning, tool calls, final answer).

A Baseline (plain RAG) question is one call: about 15k input and 2k output, so roughly a tenth of the Navigator cost.

These are estimates; real numbers come from logging `usage` once the agent runs.

## 2. Price table and estimated cost per Navigator question

| Provider / model | Input | Cached input | Output | Est. per question | Free tier |
|---|---|---|---|---|---|
| **Claude Haiku 5.5** | $0.10 | ~$0.01 | $0.50 | **~$0.01** | No |
| **Claude Sonnet 5.5** | $2.00 | $0.20 | $10.00 | **~$0.23** | No |
| Claude Opus 5.5 | $4.00 | $0.20 | $20.00 | ~$0.43 | No |
| OpenAI GPT-6 Luna | $0.10 | $0.01 | $0.50 | ~$0.01 | No |
| OpenAI GPT-6.1 Sol | $2.00 | $0.10 | $10.00 | ~$0.22 | No |
| OpenAI GPT-6 Astra | $10.00 | $1.00 | $50.00 | ~$1.15 | No |
| **Gemini 3.8 Flash** | $0.75 (→ $1.50 on 2027-01-01) | $0.075 | $3.75 (→ $7.50) | ~$0.09 (→ ~$0.17) | **Yes, rate limited** |
| Gemini 3.1 Pro Preview | $2.00 | $0.20 | $12.00 | ~$0.25 | No |
| DeepSeek (flagship chat) | ~$0.14 | n/a | ~$0.28 to $0.42 | ~$0.03 | No |
| Local model via Ollama | $0 | $0 | $0 | $0 (your hardware) | n/a |

Haiku 5.5 cached-input price is assumed at 10% of input; not confirmed.

### What that means in practice

| Usage | Haiku 5.5 / Luna | Gemini 3.8 Flash | Sonnet 5.5 / Sol |
|---|---|---|---|
| Development, ~300 questions | ~$3 | free tier, or ~$27 paid | ~$70 |
| One eval run: 4 repos x 15 questions x 2 modes (120 questions, half Navigator) | ~$1 | ~$6 | ~$15 |
| Light personal use, ~20 questions/day for a month | ~$6 | ~$55 | ~$140 |

## 3. Pros and cons

### Claude (Anthropic)
- Pros: strong at multi-step tool use and long agent loops, which is the core of the Navigator; 1M context on all three current models; prompt caching keeps the repeated-prefix cost low; Haiku 5.5 is very cheap for development and still uses the same API and tool format as Sonnet, so switching tiers is a config change.
- Cons: no free tier, needs a prepaid API account; Sonnet and Opus are among the pricier mid/top options; safety classifiers can occasionally refuse (handled with a fallback setting).

### OpenAI (GPT-6 family)
- Pros: price-competitive at every tier (Luna matches Haiku, Sol matches Sonnet); mature tool calling and structured outputs; large ecosystem.
- Cons: no free API tier; model naming and pricing have been shifting a lot this year, and the third-party sources disagree, so budgeting is less predictable; flagship Astra is expensive.

### Google Gemini
- Pros: the only major provider with a real **free tier** (Flash and Flash-Lite models), which means you can build and test the whole MVP at $0; Flash is cheap and fast; long context.
- Cons: free-tier prompts and responses are used to improve Google products (fine for public repos, which is all the MVP handles); free tier is rate limited, which slows eval runs; Google has announced price increases on 2026-12-01 and 2027-01-01, roughly doubling Flash; my assessment is that Flash-class models are less reliable than Sonnet-class models on long multi-step investigations.

### DeepSeek
- Pros: very cheap.
- Cons: pricing data found was mostly from unofficial aggregators; API availability and latency vary; data is processed in China, which matters for some users even with public repos.

### Local (Ollama, e.g. a Qwen coder model)
- Pros: free, private, works offline.
- Cons: needs a capable machine; much weaker at multi-step tool use, which would undermine the hypothesis test; slow on a laptop; adds hosting cost and complexity later.

## 4. Quality

There is no public benchmark for "investigate a repo and explain it", so the closest proxies are agentic coding benchmarks, which test long tool-use loops over real codebases. Coverage of these exact models is patchy, most numbers are vendor-reported, and the scaffolding (the agent harness) moves scores a lot. Treat these as directional.

| Model | Terminal-Bench 4.0 | SWE-Bench Pro | Notes |
|---|---|---|---|
| Claude Opus 5.5 | 64.8% (with Claude Code) | n/a | Top entry on the 4.0 board found |
| Claude Sonnet 5.5 | 61.8% (with Claude Code); 70.6% (vendor launch figure) | 81.3 | Sources disagree on Terminal-Bench |
| OpenAI GPT-6 Astra | 58.2% (with Codex) | n/a | Flagship; GPT-6.1 Sol and Luna: no comparable scores found |
| Claude Haiku 5.5 | 39.2% (max effort) | 64.8 | Vendor-reported, no independent results yet |
| Gemini 3.8 Flash | 19.1% | n/a | Scores 80.0% on a Vals SWE-bench run, which is a different, easier setup |

What this suggests:
- **Cheap tier vs. mid tier is a big gap** on long agent tasks (Haiku and Flash land well below Sonnet). For the Navigator, a weaker model means shallower investigations, missed callers, and more wrong citations.
- **Mid tier vs. flagship is a small gap.** Sonnet 5.5 is within a few points of Opus 5.5, at half the price, which is why it is the sweet spot here.
- **Gemini Flash's free tier is fine for building the plumbing**, but its numbers on the harder agentic benchmark are the weakest of the group, so it would understate what the Navigator can do.
- Cheap models are still useful as the **Baseline** model and for quick development loops, where the goal is "does the pipeline work", not answer quality.

The real answer will come from this project's own eval: once the agent works, a pilot of about 10 questions on one repo across Haiku 5.5, Sonnet 5.5 and Gemini Flash costs around $3 and shows the quality difference on exactly this task.

## 5. Recommendation

1. **Build a small provider layer** (one interface: `chat(messages, tools) -> response` with usage reporting) so the model is a config value. This also lets the eval compare providers, which is a nice extra result for the project.
2. **Develop on a cheap model**: Claude Haiku 5.5 (about a cent per question, a $5 to $10 credit lasts a long time) or Gemini 3.8 Flash on the free tier if you want to spend $0 to start.
3. **Run the real Navigator vs. Baseline comparison on one stronger model**, Claude Sonnet 5.5 by default, with GPT-6.1 Sol as an optional second data point.
4. Log token usage per question from day one so these estimates get replaced with measured numbers.

## Sources

- Claude pricing: Anthropic model table (claude-api reference, cached 2026-10-06)
- [Gemini Developer API pricing (Google)](https://ai.google.dev/gemini-api/docs/pricing)
- [OpenAI API Pricing (aipricing.guru, synced 2026-10-18)](https://www.aipricing.guru/openai-pricing/)
- [OpenAI API pricing in 2026 (CloudZero)](https://www.cloudzero.com/blog/openai-pricing/)
- [DeepSeek API Pricing and Integration (tkmxai)](https://tkmxai.it.com/deepseek-api-pricing-and-integration)
- [DeepSeek vs OpenAI API Pricing (costbench)](https://costbench.com/compare/deepseek-vs-openai-api/)
- [Terminal-Bench, tau-bench and agent leaderboards (morphllm, Oct 2026)](https://www.morphllm.com/best-ai-coding-agents-2026)
- [Claude Haiku 5.5 benchmarks (Apidog)](https://apidog.com/blog/claude-haiku-5-5-benchmarks/)
- [Gemini 3.8 Flash benchmark scores (BenchLM)](https://benchlm.ai/models/gemini-3-8-flash)
- [SWE-bench Verified leaderboard (BenchLM)](https://benchlm.ai/benchmarks/swe-bench-verified)
