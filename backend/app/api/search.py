from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.baseline import answer_baseline
from app.agent.llm import LLMUnavailable
from app.answer.links import github_url
from app.answer.schema import Answer
from app.core.db import get_session
from app.ingest.pipeline import clone_dir
from app.models import Repo, RepoStatus
from app.search.semantic import ChunkHit, SemanticUnavailable, semantic_search
from app.search.symbols import SymbolHit, symbol_search
from app.search.text import SearchError, TextMatch, text_search

router = APIRouter(prefix="/repos/{repo_id}", tags=["search"])

Session = Annotated[AsyncSession, Depends(get_session)]


async def _searchable(session: AsyncSession, repo_id: int) -> Repo:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    if repo.status not in (RepoStatus.READY, RepoStatus.EMBEDDING) or not repo.sha:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Repository is {repo.status}; it can be searched once indexing finishes.",
        )
    return repo


class TextMatchOut(BaseModel):
    path: str
    line: int
    text: str
    ranges: list[tuple[int, int]]
    url: str


class TextSearchOut(BaseModel):
    matches: list[TextMatchOut]
    truncated: bool


class SymbolOut(BaseModel):
    id: int
    kind: str
    name: str
    qualified_name: str
    path: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    is_test: bool
    url: str


class ChunkOut(BaseModel):
    id: int
    kind: str
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    score: float
    is_test: bool
    url: str


@router.get("/search/text")
async def search_text(
    repo_id: int,
    session: Session,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    regex: bool = False,
    case: Literal["smart", "sensitive", "insensitive"] = "smart",
    glob: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TextSearchOut:
    """Exact or regex search over the checkout with ripgrep."""
    repo = await _searchable(session, repo_id)
    try:
        result = await text_search(
            clone_dir(repo.owner, repo.name, repo.sha),
            q,
            regex=regex,
            case_sensitive={"smart": None, "sensitive": True, "insensitive": False}[
                case
            ],
            path_glob=glob or None,
            limit=limit,
        )
    except SearchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    def out(m: TextMatch) -> TextMatchOut:
        return TextMatchOut(**asdict(m), url=github_url(repo, m.path, m.line))

    return TextSearchOut(
        matches=[out(m) for m in result.matches], truncated=result.truncated
    )


@router.get("/search/symbols")
async def search_symbols(
    repo_id: int,
    session: Session,
    q: Annotated[str, Query(min_length=1, max_length=300)],
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SymbolOut]:
    """Find functions, classes, methods and types by name or qualified name."""
    repo = await _searchable(session, repo_id)

    def out(s: SymbolHit) -> SymbolOut:
        return SymbolOut(
            **asdict(s), url=github_url(repo, s.path, s.start_line, s.end_line)
        )

    return [
        out(s) for s in await symbol_search(session, repo.id, q, kind=kind, limit=limit)
    ]


@router.get("/search/semantic")
async def search_semantic(
    repo_id: int,
    session: Session,
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    k: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[ChunkOut]:
    """Embedding search over code chunks."""
    repo = await _searchable(session, repo_id)
    try:
        hits = await semantic_search(session, repo.id, q, k)
    except SemanticUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    def out(h: ChunkHit) -> ChunkOut:
        return ChunkOut(
            **asdict(h), url=github_url(repo, h.path, h.start_line, h.end_line)
        )

    return [out(h) for h in hits]


class AskIn(BaseModel):
    question: str
    mode: Literal["baseline", "navigator"] = "baseline"


@router.post("/ask")
async def ask(repo_id: int, body: AskIn, session: Session) -> Answer:
    """Answer a question. Baseline is plain RAG; Navigator arrives in milestone 5."""
    repo = await _searchable(session, repo_id)
    question = body.question.strip()
    if not question:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Question is empty")
    if body.mode == "navigator":
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "Navigator mode is not built yet. Use baseline for now.",
        )
    try:
        return await answer_baseline(session, repo, question)
    except (LLMUnavailable, SemanticUnavailable) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
