"""Metrics for one answer against its gold references, the LLM judge, and
the run report."""

import re
from collections import defaultdict
from statistics import mean, median
from typing import Literal

from pydantic import BaseModel, Field

# USD per 1M tokens: input, cache write, cache read, output. Cache writes are
# priced at the 5-minute rate (1.25x input); the Navigator's 1-hour prefix
# writes cost 2x, so cost is a slight underestimate for Navigator runs.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-sonnet-5-5": (2.00, 2.50, 0.20, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
}

LEAK = re.compile(r'<parameter name="|</(summary|explanation|references|gaps)>')


def cost_usd(usage: dict) -> float | None:
    price = next((p for m, p in PRICES.items() if usage["model"].startswith(m)), None)
    if price is None:
        return None
    inp, write, read, out = price
    fresh = usage["input_tokens"] - usage.get("cache_write_tokens", 0)
    return (
        fresh * inp
        + usage.get("cache_write_tokens", 0) * write
        + usage.get("cache_read_tokens", 0) * read
        + usage["output_tokens"] * out
    ) / 1_000_000


def _flow_nodes(nodes: list[dict]):
    for n in nodes:
        yield n
        yield from _flow_nodes(n.get("children", []))


def cited(answer: dict) -> tuple[set[str], set[str]]:
    """Paths and symbols the answer points the reader at: references, tests,
    flow steps and the evidence its sentences cite."""
    by_id = {e["id"]: e for e in answer.get("evidence", [])}
    locs = [*answer["references"], *answer["tests"], *_flow_nodes(answer["flow"])]
    for s in answer["explanation"]:
        locs += [by_id[e] for e in s["evidence"] if e in by_id]
    paths = {loc["path"] for loc in locs}
    symbols = {loc["symbol"] for loc in locs if loc.get("symbol")}
    return paths, symbols


def answer_text(answer: dict) -> str:
    parts = [answer["summary"], *(s["text"] for s in answer["explanation"])]
    parts += [r.get("note", "") for r in answer["references"] + answer["tests"]]
    parts += [n["symbol"] for n in _flow_nodes(answer["flow"])]
    return "\n".join(parts)


def metrics(question: dict, answer: dict) -> dict:
    gold = question["gold"]
    paths, symbols = cited(answer)
    text = answer_text(answer)
    allowed = set(gold["files"]) | set(question.get("related_files", []))

    files_hit = [f for f in gold["files"] if f in paths]
    # A gold symbol counts when a cited symbol contains it or the text names it.
    symbols_hit = [
        s for s in gold["symbols"] if any(s in c for c in symbols) or s in text
    ]
    # Paths under a related directory (e.g. "test/helpers") count as allowed.
    on_target = [
        p for p in paths if p in allowed or any(p.startswith(a + "/") for a in allowed)
    ]
    sentences = answer["explanation"]
    facts = [s for s in sentences if s["kind"] == "fact"]
    citations = answer.get("citations") or {"cited": 0, "valid": 0}
    return {
        "file_recall": len(files_hit) / len(gold["files"]),
        "symbol_recall": len(symbols_hit) / len(gold["symbols"]),
        "file_precision": len(on_target) / len(paths) if paths else None,
        "files_missed": sorted(set(gold["files"]) - set(files_hit)),
        "symbols_missed": [s for s in gold["symbols"] if s not in symbols_hit],
        "files_cited": sorted(paths),
        "citation_validity": citations["valid"] / citations["cited"]
        if citations["cited"]
        else None,
        "citations_cited": citations["cited"],
        "sentences": len(sentences),
        "facts": len(facts),
        "downgraded": sum(1 for s in sentences if s.get("downgraded")),
        "leaked": bool(LEAK.search(answer["summary"])),
        "no_answer": not sentences and not answer["references"],
    }


class JudgeScore(BaseModel):
    correctness: int = Field(description="1 to 5")
    completeness: int = Field(description="1 to 5")
    honesty: int = Field(description="1 to 5")
    verdict: Literal["correct", "partial", "wrong"]
    rationale: str


JUDGE_SYSTEM = """You grade answers to questions about a code repository. You get \
the question, a reference answer written by someone who read the code, the files \
and symbols a correct answer should point to, and the candidate answer with its \
cited locations. Each candidate sentence is tagged FACT (claimed to be shown by \
cited code) or INTERPRETATION (the system's own reasoning).

Score each from 1 (bad) to 5 (excellent):
- correctness: are the claims true according to the reference answer? Penalise \
wrong claims heavily, even if other parts are right. Do not penalise true details \
that the reference does not mention.
- completeness: does it cover the key points of the reference answer?
- honesty: are FACT tags used only for claims backed by citations, are guesses \
marked as INTERPRETATION, and does it admit what it could not find instead of \
inventing it? An answer that correctly says something is outside the repository \
scores high.

verdict: "correct" if a developer could rely on it, "partial" if useful but missing \
or wrong on something important, "wrong" otherwise. rationale: two or three \
sentences naming the main errors or omissions."""


def render_answer(answer: dict) -> str:
    lines = [f"Summary: {answer['summary']}", "", "Explanation:"]
    for s in answer["explanation"]:
        tag = "FACT" if s["kind"] == "fact" else "INTERPRETATION"
        lines.append(f"- [{tag}] {s['text']}")
    lines.append("\nReferences:")
    for r in answer["references"] + answer["tests"]:
        sym = f" {r['symbol']}" if r.get("symbol") else ""
        lines.append(
            f"- {r['path']}:{r['start_line']}-{r['end_line']}{sym}: {r['note']}"
        )
    if answer["flow"]:
        lines.append("\nFlow:")

        def walk(nodes, depth):
            for n in nodes:
                lines.append(
                    f"{'  ' * depth}- {n['symbol']} ({n['path']}): {n['note']}"
                )
                walk(n.get("children", []), depth + 1)

        walk(answer["flow"], 0)
    if answer["gaps"]:
        lines.append("\nGaps:")
        lines += [f"- {g}" for g in answer["gaps"]]
    return "\n".join(lines)


async def judge(llm, model: str, question: dict, answer: dict) -> dict:
    prompt = (
        f"<question>{question['question']}</question>\n\n"
        f"<reference_answer>{question['expected']}</reference_answer>\n\n"
        f"<gold_files>{', '.join(question['gold']['files'])}</gold_files>\n"
        f"<gold_symbols>{', '.join(question['gold']['symbols'])}</gold_symbols>\n\n"
        f"<candidate_answer>\n{render_answer(answer)}\n</candidate_answer>"
    )
    response = await llm.messages.parse(
        model=model,
        max_tokens=4000,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=JudgeScore,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"Judge returned no score ({response.stop_reason})")
    out = response.parsed_output.model_dump()
    out["judge_model"] = response.model
    out["judge_cost_usd"] = cost_usd(
        {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
    )
    return out


def _avg(values) -> float | None:
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def summarize(rows: list[dict]) -> dict:
    """Aggregate per mode, and per mode and category."""

    def agg(group: list[dict]) -> dict:
        ok = [r for r in group if r.get("answer")]
        m = [r["metrics"] for r in ok]
        j = [r["judge"] for r in ok if r.get("judge")]
        lat = [r["answer"]["usage"]["latency_ms"] / 1000 for r in ok]
        return {
            "questions": len(group),
            "errors": len(group) - len(ok),
            "file_recall": _avg(x["file_recall"] for x in m),
            "symbol_recall": _avg(x["symbol_recall"] for x in m),
            "file_precision": _avg(x["file_precision"] for x in m),
            "citation_validity": _avg(x["citation_validity"] for x in m),
            "downgraded_facts": sum(x["downgraded"] for x in m),
            "leaked_summaries": sum(x["leaked"] for x in m),
            "no_answer": sum(x["no_answer"] for x in m),
            "correctness": _avg(x["correctness"] for x in j),
            "completeness": _avg(x["completeness"] for x in j),
            "honesty": _avg(x["honesty"] for x in j),
            "verdicts": {
                v: sum(1 for x in j if x["verdict"] == v)
                for v in ("correct", "partial", "wrong")
            },
            "latency_median_s": median(lat) if lat else None,
            "latency_max_s": max(lat) if lat else None,
            "tool_calls_avg": _avg(r["answer"]["usage"]["tool_calls"] for r in ok),
            "cost_usd_total": sum(r.get("cost_usd") or 0 for r in ok),
            "cost_usd_avg": _avg(r.get("cost_usd") for r in ok),
        }

    by_mode = defaultdict(list)
    by_cat = defaultdict(list)
    by_repo = defaultdict(list)
    for r in rows:
        by_mode[r["mode"]].append(r)
        by_cat[(r["mode"], r["category"])].append(r)
        by_repo[(r["mode"], r["repo"])].append(r)
    return {
        "modes": {m: agg(g) for m, g in by_mode.items()},
        "categories": {f"{m}/{c}": agg(g) for (m, c), g in sorted(by_cat.items())},
        "repos": {f"{m}/{p}": agg(g) for (m, p), g in sorted(by_repo.items())},
    }


def _fmt(v, pct=False, digits=2) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.0f}%"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def report(meta: dict, summary: dict, rows: list[dict]) -> str:
    modes = list(summary["modes"])
    lines = [
        f"# Evaluation run {meta['run_id']}",
        "",
        f"- Answer model: `{meta['model']}`, Navigator effort `{meta['effort']}`",
        f"- Judge model: `{meta['judge_model'] or 'none'}`",
        f"- Questions: {meta['questions']} across {', '.join(meta['repos'])}",
        "",
        "## Overall",
        "",
        "| Metric | " + " | ".join(modes) + " |",
        "|---|" + "---|" * len(modes),
    ]
    rows_spec = [
        ("File recall", "file_recall", True),
        ("Symbol recall", "symbol_recall", True),
        ("File precision", "file_precision", True),
        ("Citation validity", "citation_validity", True),
        ("Judge correctness (1-5)", "correctness", False),
        ("Judge completeness (1-5)", "completeness", False),
        ("Judge honesty (1-5)", "honesty", False),
        ("Facts downgraded (no valid citation)", "downgraded_facts", False),
        ("Leaked summaries", "leaked_summaries", False),
        ("Answers with no content", "no_answer", False),
        ("Errors", "errors", False),
        ("Median latency (s)", "latency_median_s", False),
        ("Max latency (s)", "latency_max_s", False),
        ("Avg tool calls", "tool_calls_avg", False),
        ("Avg cost per question (USD)", "cost_usd_avg", False),
        ("Total cost (USD)", "cost_usd_total", False),
    ]
    for label, key, pct in rows_spec:
        digits = 4 if key.startswith("cost") else 2
        vals = [_fmt(summary["modes"][m][key], pct, digits) for m in modes]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    lines.append(
        "| Verdicts correct / partial / wrong | "
        + " | ".join(
            "{correct} / {partial} / {wrong}".format(**summary["modes"][m]["verdicts"])
            for m in modes
        )
        + " |"
    )

    for title, key in (("By category", "categories"), ("By repo", "repos")):
        lines += [
            "",
            f"## {title}",
            "",
            "| Group | File recall | Symbol recall | Precision | Correctness | Completeness | Honesty |",
            "|---|---|---|---|---|---|---|",
        ]
        for name, a in summary[key].items():
            lines.append(
                f"| {name} | {_fmt(a['file_recall'], True)} | {_fmt(a['symbol_recall'], True)} "
                f"| {_fmt(a['file_precision'], True)} | {_fmt(a['correctness'])} "
                f"| {_fmt(a['completeness'])} | {_fmt(a['honesty'])} |"
            )

    lines += [
        "",
        "## Per question",
        "",
        "| Question | Mode | Files | Symbols | Verdict | Missed | Judge notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: (r["id"], r["mode"])):
        if not r.get("answer"):
            lines.append(f"| {r['id']} | {r['mode']} | | | error | | {r['error']} |")
            continue
        m, j = r["metrics"], r.get("judge") or {}
        missed = ", ".join(m["files_missed"] + m["symbols_missed"])
        note = (j.get("rationale") or "").replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {r['id']} | {r['mode']} | {_fmt(m['file_recall'], True)} "
            f"| {_fmt(m['symbol_recall'], True)} | {j.get('verdict', 'n/a')} "
            f"| {missed} | {note} |"
        )
    return "\n".join(lines) + "\n"
