import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm import get_llm
from app.answer.links import github_url
from app.answer.schema import Answer, Evidence, ModelAnswer, Usage
from app.answer.validate import count_citations, validate_answer
from app.core.config import get_settings
from app.models import Repo
from app.search.semantic import semantic_search

log = logging.getLogger(__name__)

MAX_TOKENS = 8000

SYSTEM = """You answer questions about the GitHub repository {repo} at commit {sha}.

You get one question and a set of code excerpts retrieved by semantic search. \
Each excerpt has an evidence id, a path, an optional symbol and numbered lines. \
Answer only from these excerpts.

Rules:
- summary: one or two sentences that answer the question directly.
- explanation: short sentences. Mark a sentence "fact" only when the cited \
excerpts show it directly, and cite their evidence ids. Mark reasoning, \
intent or anything inferred as "interpretation", still citing what it rests on.
- references: the excerpts a reader should open, most important first. Narrow \
start_line and end_line to the relevant lines inside that excerpt and say in \
note why it matters.
- gaps: what the question needs that the excerpts do not show. If the excerpts \
do not answer the question, say so in summary and gaps instead of guessing.
- Cite only evidence ids that appear below. Never invent paths or line numbers."""


def _numbered(content: str, start: int) -> str:
    return "\n".join(
        f"{start + i:>5} {line}" for i, line in enumerate(content.split("\n"))
    )


def _context(evidence: list[Evidence]) -> str:
    blocks = []
    for e in evidence:
        symbol = f' symbol="{e.symbol}"' if e.symbol else ""
        blocks.append(
            f'<evidence id="{e.id}" path="{e.path}"{symbol} '
            f'lines="{e.start_line}-{e.end_line}">\n'
            f"{_numbered(e.content, e.start_line)}\n</evidence>"
        )
    return "\n\n".join(blocks)


async def answer_baseline(session: AsyncSession, repo: Repo, question: str) -> Answer:
    """Plain RAG: embed the question, take the top-k chunks, one LLM call."""
    settings = get_settings()
    llm = get_llm()
    started = time.monotonic()

    hits = await semantic_search(session, repo.id, question, k=settings.baseline_top_k)
    evidence = [
        Evidence(
            id=f"e{i}",
            path=h.path,
            symbol=h.symbol,
            start_line=h.start_line,
            end_line=h.end_line,
            url=github_url(repo, h.path, h.start_line, h.end_line),
            content=h.content,
            score=h.score,
            is_test=h.is_test,
        )
        for i, h in enumerate(hits, start=1)
    ]
    log.info("Baseline retrieved %d chunks, calling %s", len(hits), settings.llm_model)

    response = await llm.messages.parse(
        model=settings.llm_model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM.format(repo=f"{repo.owner}/{repo.name}", sha=repo.sha),
        messages=[
            {
                "role": "user",
                "content": f"{_context(evidence)}\n\n<question>{question}</question>",
            }
        ],
        output_format=ModelAnswer,
    )
    log.info(
        "Baseline answer: stop %s, %d in / %d out tokens, %d ms",
        response.stop_reason,
        response.usage.input_tokens,
        response.usage.output_tokens,
        (time.monotonic() - started) * 1000,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined to answer this question.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise RuntimeError("The model's answer was cut off or malformed. Try again.")

    ledger = {e.id: e for e in evidence}
    sentences, references = validate_answer(
        response.parsed_output,
        ledger,
        lambda path, s, e: github_url(repo, path, s, e),
    )
    return Answer(
        mode="baseline",
        question=question,
        summary=response.parsed_output.summary,
        explanation=sentences,
        references=[r for r in references if not r.is_test],
        tests=[r for r in references if r.is_test],
        gaps=response.parsed_output.gaps,
        evidence=evidence,
        citations=count_citations(response.parsed_output, ledger),
        usage=Usage(
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=round((time.monotonic() - started) * 1000),
        ),
    )
