"""Run the benchmark: ask every question in both modes, score the answers
against gold references, grade them with an LLM judge and write a report.

Runs in-process against the backend code and database (the dev server does not
need to be running), so the answer model can differ from the server's:

    cd backend && uv run python ../eval/run.py --model claude-sonnet-5-5
    cd backend && uv run python ../eval/run.py --repos click --ids click-01 --no-judge
    cd backend && uv run python ../eval/run.py --rescore ../eval/results/<run>
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
QUESTIONS_DIR = EVAL_DIR / "questions"
RESULTS_DIR = EVAL_DIR / "results"
MODES = ("navigator", "baseline")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repos", nargs="*", help="Question files to run (default: all)")
    p.add_argument("--ids", nargs="*", help="Only these question ids")
    p.add_argument("--modes", nargs="*", default=list(MODES), choices=MODES)
    p.add_argument("--model", help="Answer model (default: LLM_MODEL from .env)")
    p.add_argument("--effort", help="Navigator effort (default: AGENT_EFFORT)")
    p.add_argument("--judge-model", default="claude-haiku-5-5")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--rescore", type=Path, help="Re-score and re-judge a past run")
    return p.parse_args()


ARGS = parse_args()
# Settings are cached on first read, so overrides go in before app imports.
if ARGS.model:
    os.environ["LLM_MODEL"] = ARGS.model
if ARGS.effort:
    os.environ["AGENT_EFFORT"] = ARGS.effort
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(EVAL_DIR))

from score import cost_usd, judge, metrics, report, summarize
from sqlalchemy import select

from app.agent.baseline import answer_baseline
from app.agent.llm import get_llm
from app.agent.navigator import navigate
from app.core.config import get_settings
from app.core.db import SessionLocal, engine
from app.models import Repo


def load_questions() -> list[dict]:
    out = []
    for path in sorted(QUESTIONS_DIR.glob("*.json")):
        if ARGS.repos and path.stem not in ARGS.repos:
            continue
        data = json.loads(path.read_text())
        for q in data["questions"]:
            if ARGS.ids and q["id"] not in ARGS.ids:
                continue
            out.append({**q, "repo": data["repo"]})
    return out


async def find_repos(names: set[str]) -> dict[str, Repo]:
    async with SessionLocal() as session:
        repos = {}
        for full in names:
            owner, name = full.split("/")
            repo = await session.scalar(
                select(Repo).where(Repo.owner == owner, Repo.name == name)
            )
            if repo is None or repo.status != "ready":
                raise SystemExit(f"{full} is not indexed and ready; ingest it first.")
            repos[full] = repo
    return repos


async def ask(repo: Repo, question: str, mode: str) -> dict:
    async with SessionLocal() as session:
        if mode == "baseline":
            answer = await answer_baseline(session, repo, question)
        else:
            answer = None
            async for event in navigate(session, repo, question):
                if event.kind == "answer":
                    answer = event.data
            if answer is None:
                raise RuntimeError("No answer was produced")
    return answer.model_dump(mode="json")


async def run_one(q: dict, mode: str, repo: Repo, sem, judge_model, out) -> dict:
    row = {"id": q["id"], "repo": q["repo"], "category": q["category"], "mode": mode}
    async with sem:
        started = time.monotonic()
        try:
            row["answer"] = await ask(repo, q["question"], mode)
        except Exception as exc:  # noqa: BLE001 (recorded, the run carries on)
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["wall_s"] = round(time.monotonic() - started, 1)
            print(f"  {q['id']} {mode}: ERROR {row['error']}", flush=True)
            out.write(json.dumps(row) + "\n")
            out.flush()
            return row
        row["wall_s"] = round(time.monotonic() - started, 1)
        await score_row(row, q, judge_model)
    m = row["metrics"]
    verdict = row.get("judge", {}).get("verdict", "-")
    print(
        f"  {q['id']} {mode}: files {m['file_recall']:.0%} symbols "
        f"{m['symbol_recall']:.0%} {verdict} {row['wall_s']}s",
        flush=True,
    )
    out.write(json.dumps(row) + "\n")
    out.flush()
    return row


async def score_row(row: dict, q: dict, judge_model: str | None) -> None:
    row["metrics"] = metrics(q, row["answer"])
    row["cost_usd"] = cost_usd(row["answer"]["usage"])
    row.pop("judge", None)
    if judge_model:
        try:
            row["judge"] = await judge(get_llm(), judge_model, q, row["answer"])
        except Exception as exc:  # noqa: BLE001
            row["judge_error"] = f"{type(exc).__name__}: {exc}"


def write_outputs(run_dir: Path, meta: dict, rows: list[dict]) -> None:
    summary = summarize(rows)
    (run_dir / "summary.json").write_text(
        json.dumps({"meta": meta, **summary}, indent=2) + "\n"
    )
    (run_dir / "report.md").write_text(report(meta, summary, rows))


async def rescore(run_dir: Path, judge_model: str | None) -> None:
    meta = json.loads((run_dir / "summary.json").read_text())["meta"]
    meta["judge_model"] = judge_model
    by_id = {q["id"]: q for q in load_questions()}
    rows = [json.loads(line) for line in (run_dir / "answers.jsonl").open()]
    rows = [r for r in rows if r["id"] in by_id]
    sem = asyncio.Semaphore(ARGS.concurrency)

    async def one(r):
        if r.get("answer"):
            async with sem:
                await score_row(r, by_id[r["id"]], judge_model)

    await asyncio.gather(*(one(r) for r in rows))
    with (run_dir / "answers.jsonl").open("w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    write_outputs(run_dir, meta, rows)
    print(f"Rescored {len(rows)} answers: {run_dir / 'report.md'}")


async def main() -> None:
    try:
        await _main()
    finally:
        await engine.dispose()


async def _main() -> None:
    judge_model = None if ARGS.no_judge else ARGS.judge_model
    if ARGS.rescore:
        await rescore(ARGS.rescore.resolve(), judge_model)
        return

    settings = get_settings()
    questions = load_questions()
    if not questions:
        raise SystemExit("No questions matched.")
    repos = await find_repos({q["repo"] for q in questions})

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}_{settings.llm_model}"
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "model": settings.llm_model,
        "effort": settings.agent_effort,
        "max_tool_calls": settings.agent_max_tool_calls,
        "baseline_top_k": settings.baseline_top_k,
        "judge_model": judge_model,
        "modes": ARGS.modes,
        "questions": len(questions),
        "repos": sorted(repos),
        "started_at": datetime.now(UTC).isoformat(),
    }
    print(
        f"Run {run_id}: {len(questions)} questions x {len(ARGS.modes)} modes, "
        f"judge {judge_model or 'off'}",
        flush=True,
    )

    sem = asyncio.Semaphore(ARGS.concurrency)
    with (run_dir / "answers.jsonl").open("w") as out:
        rows = await asyncio.gather(
            *(
                run_one(q, mode, repos[q["repo"]], sem, judge_model, out)
                for q in questions
                for mode in ARGS.modes
            )
        )
    meta["finished_at"] = datetime.now(UTC).isoformat()
    write_outputs(run_dir, meta, rows)
    print(f"Report: {run_dir / 'report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
