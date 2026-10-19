"""The Navigator: a Claude tool loop that investigates the code, records
evidence in a ledger and finishes with a structured, validated answer."""

import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ledger import Ledger
from app.agent.llm import get_llm
from app.agent.tools import ALL_TOOLS, Toolbox, label_for
from app.answer.links import github_url
from app.answer.schema import (
    Answer,
    AnswerReference,
    Evidence,
    NavigatorModelAnswer,
    Step,
    Usage,
)
from app.answer.validate import validate_answer, validate_flow, validate_references
from app.core.config import get_settings
from app.ingest.pipeline import clone_dir
from app.models import Repo

MAX_TOKENS = 16000
MAX_NUDGES = 2
# Tools + system and conversation history are reused across questions that
# can be minutes apart, so they get the 1-hour TTL. The tool-loop tail is
# re-read within seconds and uses the default 5 minutes via the top-level
# automatic breakpoint. Longer TTLs must come first in the prompt.
CACHE_1H = {"type": "ephemeral", "ttl": "1h"}

SYSTEM = """You are the Navigator. You answer questions about one GitHub \
repository by investigating its code with tools, then submit a structured, \
evidence-backed answer with the submit_answer tool.

Tools return compact JSON. Every code location in a result carries an \
evidence_id (e.g. "e12"). Those ids are the only things you may cite.

How to investigate:
- Understanding ("what does this repo do", "how does X work"): start broad \
(repo_overview, semantic_search or symbol_search), pick the entry points, \
follow get_callees, read the key code with get_definition or read_code, then \
find_tests.
- Location ("where is X"): symbol_search or text_search, confirm with \
get_definition, stop.
- Relationships ("what calls X", "what uses X"): resolve the symbol, then \
get_callers, find_references, get_imports or get_importers.
- Flow ("what happens when ..."): find the entry point and follow callees \
step by step, reading each step's code.
- Testing ("how is X tested"): find_tests for the symbol and its file, then \
read the most relevant tests.
- Prefer exact names over guesses. If a symbol name is ambiguous, results \
list several targets with ids; pass "id:<n>" to pick one.
- Do not repeat a call you already made. Several independent calls can go \
in one turn.
- Stop as soon as the evidence answers the question. Simple questions need \
two or three calls.

Code-intelligence edges are static and heuristic. Each has a confidence: \
high (resolved through imports or the class), medium (a likely match), low \
(a name guess or a text match). When a claim rests on medium or low edges, \
say so in the sentence and keep it an interpretation unless the code you \
read confirms it. Dynamic dispatch, callbacks and dependency injection are \
not resolved, so missing callers do not prove there are none.

The answer (submit_answer):
- summary: one or two sentences that answer the question directly.
- explanation: short sentences. "fact" only when the cited evidence shows it \
directly; cite every evidence id it rests on. Reasoning, intent, design \
rationale or anything inferred is "interpretation", still citing what it \
rests on.
- flow: for flow and "how does X work" questions, the call path as a flat \
tree. Number steps from 1; parent is the caller's step number, -1 for a \
root. Each step cites the evidence for that symbol. Leave it empty for \
other questions.
- references: the locations a reader should open, most important first. \
Narrow start_line and end_line to the relevant lines inside the cited \
evidence, and say in note why each matters. Put test code in tests instead.
- tests: tests that exercise the code in question, in the same shape.
- gaps: what the question needs that you could not establish. If the code \
does not answer the question, say so in summary and gaps instead of guessing.
- Never invent paths, line numbers or evidence ids. Links are built for you.

Call submit_answer exactly once, as your final action. Do not answer in \
plain text."""

NUDGE = (
    "Submit your answer now by calling the submit_answer tool. "
    "Do not reply in plain text."
)
BUDGET_NOTE = (
    "The investigation budget is used up. No more tools will run. Call "
    "submit_answer now with what you have, and list what is missing in gaps."
)


SYSTEM_BLOCKS = [{"type": "text", "text": SYSTEM, "cache_control": CACHE_1H}]


class AgentError(Exception):
    """A failure to show the user in place of an answer."""


@dataclass
class PriorTurn:
    question: str
    mode: str
    answer: dict[str, Any] | None


@dataclass
class Event:
    kind: str  # "step" or "answer"
    data: Step | Answer


@dataclass
class _Usage:
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    total_input: int = 0

    def add(self, response) -> None:
        u = response.usage
        self.model = response.model
        cache_read = u.cache_read_input_tokens or 0
        cache_write = u.cache_creation_input_tokens or 0
        self.input_tokens += u.input_tokens + cache_write
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.output_tokens += u.output_tokens
        self.total_input += u.input_tokens + cache_write + cache_read
        self.model_calls += 1


def _intro(repo: Repo, question: str, history: list[PriorTurn]) -> list[dict]:
    """The first user message as content blocks. Each earlier turn is its own
    block and the last one carries a cache breakpoint, so a follow-up reads
    the history the previous follow-up already wrote and adds only its turn."""
    blocks = [
        {
            "type": "text",
            "text": f"<repository>{repo.owner}/{repo.name} at commit {repo.sha}</repository>",
        }
    ]
    turns = []
    for t in history:
        if t.answer is None:
            continue
        lines = [
            f"<question>{t.question}</question>",
            f"Answer: {t.answer.get('summary', '')}",
        ]
        if t.mode == "navigator":
            for s in t.answer.get("explanation", []):
                ids = ",".join(s.get("evidence", []))
                lines.append(f"- [{s.get('kind')}] {s.get('text')} ({ids})")
            cited = [
                f"{e['id']} {e['path']}:{e['start_line']}-{e['end_line']}"
                + (f" {e['symbol']}" if e.get("symbol") else "")
                for e in t.answer.get("evidence", [])
            ]
            if cited:
                lines.append("cited: " + "; ".join(cited))
        turns.append("<turn>\n" + "\n".join(lines) + "\n</turn>")
    question_text = f"<question>{question}</question>"
    if turns:
        blocks.append(
            {
                "type": "text",
                "text": "<previous_turns>\nEarlier questions in this conversation. "
                "Evidence ids cited there are still valid and can be cited again; "
                "read the code again if you need its contents.",
            }
        )
        blocks += [{"type": "text", "text": t} for t in turns]
        blocks[-1]["cache_control"] = CACHE_1H
        question_text = "</previous_turns>\n\n" + question_text
    blocks.append({"type": "text", "text": question_text})
    return blocks


def _cited(answer: Answer) -> list[str]:
    ids: list[str] = []
    for s in answer.explanation:
        ids += s.evidence
    ids += [r.evidence for r in answer.references + answer.tests]

    def walk(nodes):
        for n in nodes:
            ids.append(n.evidence)
            walk(n.children)

    walk(answer.flow)
    return list(dict.fromkeys(ids))


def _build_answer(
    repo: Repo,
    question: str,
    model: NavigatorModelAnswer,
    ledger: Ledger,
    trace: list[Step],
    usage: _Usage,
    started: float,
) -> Answer:
    def url_for(path, s, e):
        return github_url(repo, path, s, e)

    entries = ledger.entries
    sentences, references = validate_answer(model, entries, url_for)
    tests = validate_references(model.tests, entries, url_for)
    seen = {(t.path, t.start_line, t.end_line) for t in tests}
    moved: list[AnswerReference] = []
    for r in references:
        if r.is_test and (r.path, r.start_line, r.end_line) not in seen:
            moved.append(r)
    answer = Answer(
        mode="navigator",
        question=question,
        summary=model.summary,
        explanation=sentences,
        references=[r for r in references if not r.is_test],
        tests=tests + moved,
        gaps=model.gaps,
        evidence=[],
        flow=validate_flow(model.flow, entries, url_for),
        trace=trace,
        evidence_total=len(ledger),
        usage=Usage(
            model=usage.model or get_settings().llm_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            tool_calls=usage.tool_calls,
            model_calls=usage.model_calls,
            latency_ms=round((time.monotonic() - started) * 1000),
        ),
    )
    answer.evidence = [entries[i] for i in _cited(answer) if i in entries]
    return answer


_LEAKED_PARAM = re.compile(
    r'<parameter name="(\w+)">(.*?)(?=</parameter>|<parameter name="|$)', re.DOTALL
)


def _repair(args: Any) -> Any:
    """The model sometimes writes later fields into a string field as
    `</summary><parameter name="explanation">[...]`. Cut the string at the
    leak and use the leaked values for fields that came back empty."""
    if not isinstance(args, dict):
        return args
    out = dict(args)
    for key, value in args.items():
        if not isinstance(value, str) or '<parameter name="' not in value:
            continue
        head = value.split('<parameter name="', 1)[0]
        out[key] = re.sub(rf"\s*</{key}>\s*$", "", head).strip()
        for name, raw in _LEAKED_PARAM.findall(value):
            if name == key or out.get(name):
                continue
            try:
                out[name] = json.loads(raw.strip())
            except ValueError:
                if isinstance(args.get(name), str):
                    out[name] = raw.strip()
    return out


def _fallback(text: str) -> NavigatorModelAnswer:
    return NavigatorModelAnswer(
        summary=text.strip()
        or "The investigation stopped before an answer was submitted.",
        explanation=[],
        flow=[],
        references=[],
        tests=[],
        gaps=[
            "The agent did not submit a structured answer, so nothing here is cited."
        ],
    )


async def navigate(
    session: AsyncSession,
    repo: Repo,
    question: str,
    history: list[PriorTurn] = (),
    prior_evidence: list[Evidence] = (),
    ledger_out: list[Ledger] | None = None,
) -> AsyncIterator[Event]:
    """Run the tool loop, yielding a step per tool call and then the answer.
    ledger_out, when given, receives the ledger so callers can store it."""
    settings = get_settings()
    llm = get_llm()
    started = time.monotonic()
    ledger = Ledger(
        repo, clone_dir(repo.owner, repo.name, repo.sha), list(prior_evidence)
    )
    if ledger_out is not None:
        ledger_out.append(ledger)
    toolbox = Toolbox(session, repo, ledger)
    usage = _Usage()
    trace: list[Step] = []
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _intro(repo, question, list(history))}
    ]
    nudges = 0
    exhausted = False
    last_text = ""
    max_model_calls = settings.agent_max_tool_calls + MAX_NUDGES + 4

    while True:
        if usage.model_calls >= max_model_calls:
            yield Event(
                "answer",
                _build_answer(
                    repo, question, _fallback(last_text), ledger, trace, usage, started
                ),
            )
            return
        response = await llm.messages.create(
            model=settings.llm_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_BLOCKS,
            tools=ALL_TOOLS,
            tool_choice={"type": "auto"},
            messages=messages,
            output_config={"effort": settings.agent_effort},
            cache_control={"type": "ephemeral"},
        )
        usage.add(response)
        if response.stop_reason == "refusal":
            raise AgentError("The model declined to answer this question.")
        # Thinking blocks go back unchanged, as the API requires with tool use.
        messages.append({"role": "assistant", "content": response.content})
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        last_text = text or last_text
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        submit = next((b for b in tool_uses if b.name == "submit_answer"), None)
        if submit is not None:
            try:
                model = NavigatorModelAnswer.model_validate(_repair(submit.input))
            except ValidationError as exc:
                if nudges >= MAX_NUDGES:
                    model = _fallback(last_text)
                else:
                    nudges += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": b.id,
                                    "content": f"Invalid answer: {exc.errors()[:3]}"
                                    if b is submit
                                    else "Not run: submit_answer was called in the same turn.",
                                    "is_error": True,
                                }
                                for b in tool_uses
                            ],
                        }
                    )
                    continue
            yield Event(
                "answer",
                _build_answer(repo, question, model, ledger, trace, usage, started),
            )
            return

        if not tool_uses:
            if nudges >= MAX_NUDGES:
                yield Event(
                    "answer",
                    _build_answer(
                        repo,
                        question,
                        _fallback(last_text),
                        ledger,
                        trace,
                        usage,
                        started,
                    ),
                )
                return
            nudges += 1
            messages.append({"role": "user", "content": NUDGE})
            continue

        results: list[dict[str, Any]] = []
        for b in tool_uses:
            if exhausted or usage.tool_calls >= settings.agent_max_tool_calls:
                exhausted = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": BUDGET_NOTE,
                        "is_error": True,
                    }
                )
                continue
            usage.tool_calls += 1
            args = b.input if isinstance(b.input, dict) else {}
            t0 = time.monotonic()
            out = await toolbox.run(b.name, args)
            step = Step(
                index=len(trace) + 1,
                tool=b.name,
                input=args,
                label=out.label or label_for(b.name, args),
                summary=out.summary,
                evidence=out.evidence,
                error=out.error,
                duration_ms=round((time.monotonic() - t0) * 1000),
            )
            trace.append(step)
            yield Event("step", step)
            result = {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": out.content,
            }
            if out.error:
                result["is_error"] = True
            results.append(result)

        if usage.total_input >= settings.agent_max_input_tokens:
            exhausted = True
        if exhausted or usage.tool_calls >= settings.agent_max_tool_calls:
            exhausted = True
            results.append({"type": "text", "text": BUDGET_NOTE})
        messages.append({"role": "user", "content": results})
