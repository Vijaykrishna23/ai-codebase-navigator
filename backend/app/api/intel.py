from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.answer.links import github_url
from app.api.search import SymbolOut, _searchable
from app.core.db import get_session
from app.ingest.pipeline import clone_dir
from app.intel import graph
from app.intel.graph import EdgeHit, End
from app.models import File, Repo
from app.search.symbols import SymbolHit

router = APIRouter(prefix="/repos/{repo_id}/intel", tags=["intel"])

Session = Annotated[AsyncSession, Depends(get_session)]
SymbolQ = Annotated[str | None, Query(max_length=300)]
SymbolIdQ = Annotated[int | None, Query(alias="id")]
PathQ = Annotated[str, Query(min_length=1, max_length=1000)]
Confidence = Literal["high", "medium", "low"]
Limit = Annotated[int, Query(ge=1, le=500)]


class EndOut(BaseModel):
    path: str | None
    name: str
    symbol_id: int | None
    kind: str | None
    qualified_name: str | None
    start_line: int | None
    end_line: int | None
    is_test: bool
    external: bool
    url: str | None


class EdgeOut(BaseModel):
    kind: str
    confidence: str
    src: EndOut
    dst: EndOut
    line: int | None
    label: str | None
    text: str | None
    url: str  # where the relationship happens, in the source file


class GraphOut(BaseModel):
    targets: list[SymbolOut]
    edges: list[EdgeOut]
    truncated: bool


class FileGraphOut(BaseModel):
    path: str
    edges: list[EdgeOut]
    truncated: bool


class DefinitionOut(BaseModel):
    symbol: SymbolOut
    parent: str | None
    source: str
    truncated: bool


class HierarchyOut(BaseModel):
    targets: list[SymbolOut]
    bases: list[EdgeOut]
    subclasses: list[EdgeOut]
    truncated: bool


def _symbol_out(repo: Repo, s: SymbolHit) -> SymbolOut:
    return SymbolOut(
        **asdict(s), url=github_url(repo, s.path, s.start_line, s.end_line)
    )


def _end_out(repo: Repo, e: End) -> EndOut:
    url = None
    if e.path is not None:
        url = github_url(repo, e.path, e.start_line, e.end_line)
    return EndOut(**asdict(e), external=e.external, url=url)


def _edge_out(repo: Repo, h: EdgeHit) -> EdgeOut:
    path = h.src.path or ""
    return EdgeOut(
        kind=h.kind,
        confidence=h.confidence,
        src=_end_out(repo, h.src),
        dst=_end_out(repo, h.dst),
        line=h.line,
        label=h.label,
        text=h.text,
        url=github_url(repo, path, h.line),
    )


async def _targets(
    session: AsyncSession, repo: Repo, symbol: str | None, symbol_id: int | None
) -> list[SymbolHit]:
    if symbol_id is None and not (symbol or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Pass a symbol name or an id"
        )
    targets = await graph.find_symbols(session, repo.id, symbol, symbol_id)
    if not targets:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No symbol named {symbol!r}. Try symbol search for partial names.",
        )
    return targets


async def _file(session: AsyncSession, repo: Repo, path: str) -> File:
    file = await graph.file_by_path(session, repo.id, path)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No file at {path!r}")
    return file


def _graph(repo: Repo, targets, result) -> GraphOut:
    hits, truncated = result
    return GraphOut(
        targets=[_symbol_out(repo, t) for t in targets],
        edges=[_edge_out(repo, h) for h in hits],
        truncated=truncated,
    )


@router.get("/definition")
async def definition(
    repo_id: int, session: Session, symbol: SymbolQ = None, symbol_id: SymbolIdQ = None
) -> list[DefinitionOut]:
    """Where a symbol is defined, with its source (up to 300 lines)."""
    repo = await _searchable(session, repo_id)
    targets = await _targets(session, repo, symbol, symbol_id)
    checkout = clone_dir(repo.owner, repo.name, repo.sha)
    return [
        DefinitionOut(
            symbol=_symbol_out(repo, d.symbol),
            parent=d.parent,
            source=d.source,
            truncated=d.truncated,
        )
        for d in await graph.definitions(session, checkout, targets)
    ]


@router.get("/callers")
async def callers(
    repo_id: int,
    session: Session,
    symbol: SymbolQ = None,
    symbol_id: SymbolIdQ = None,
    min_confidence: Confidence = "low",
    limit: Limit = 100,
) -> GraphOut:
    """Functions and methods that call the symbol."""
    repo = await _searchable(session, repo_id)
    targets = await _targets(session, repo, symbol, symbol_id)
    result = await graph.callers(
        session, repo.id, targets, min_confidence=min_confidence, limit=limit
    )
    return _graph(repo, targets, result)


@router.get("/callees")
async def callees(
    repo_id: int,
    session: Session,
    symbol: SymbolQ = None,
    symbol_id: SymbolIdQ = None,
    min_confidence: Confidence = "low",
    limit: Limit = 100,
) -> GraphOut:
    """What the symbol calls, including calls into external packages."""
    repo = await _searchable(session, repo_id)
    targets = await _targets(session, repo, symbol, symbol_id)
    result = await graph.callees(
        session, repo.id, targets, min_confidence=min_confidence, limit=limit
    )
    return _graph(repo, targets, result)


@router.get("/references")
async def references(
    repo_id: int,
    session: Session,
    symbol: SymbolQ = None,
    symbol_id: SymbolIdQ = None,
    min_confidence: Confidence = "low",
    text: bool = True,
    limit: Limit = 200,
) -> GraphOut:
    """Every resolved usage of the symbol, plus identifier text matches (low)."""
    repo = await _searchable(session, repo_id)
    targets = await _targets(session, repo, symbol, symbol_id)
    result = await graph.references(
        session,
        repo.id,
        clone_dir(repo.owner, repo.name, repo.sha),
        targets,
        text_fallback=text,
        min_confidence=min_confidence,
        limit=limit,
    )
    return _graph(repo, targets, result)


@router.get("/imports")
async def imports(
    repo_id: int, session: Session, path: PathQ, limit: Limit = 200
) -> FileGraphOut:
    """Modules the file imports. External packages have no destination path."""
    repo = await _searchable(session, repo_id)
    file = await _file(session, repo, path)
    hits, truncated = await graph.imports_of(session, repo.id, file, limit=limit)
    return FileGraphOut(
        path=file.path, edges=[_edge_out(repo, h) for h in hits], truncated=truncated
    )


@router.get("/importers")
async def importers(
    repo_id: int, session: Session, path: PathQ, limit: Limit = 200
) -> FileGraphOut:
    """Files that import the file or names defined in it."""
    repo = await _searchable(session, repo_id)
    file = await _file(session, repo, path)
    hits, truncated = await graph.importers_of(session, repo.id, file, limit=limit)
    return FileGraphOut(
        path=file.path, edges=[_edge_out(repo, h) for h in hits], truncated=truncated
    )


@router.get("/tests")
async def tests(
    repo_id: int,
    session: Session,
    symbol: SymbolQ = None,
    symbol_id: SymbolIdQ = None,
    path: Annotated[str | None, Query(max_length=1000)] = None,
    min_confidence: Confidence = "low",
    limit: Limit = 100,
) -> GraphOut:
    """Tests that exercise a symbol (by id or name) or a file (by path)."""
    repo = await _searchable(session, repo_id)
    if path and symbol_id is None and not symbol:
        file = await _file(session, repo, path)
        result = await graph.tests_for(
            session, repo.id, file=file, min_confidence=min_confidence, limit=limit
        )
        return _graph(repo, [], result)
    targets = await _targets(session, repo, symbol, symbol_id)
    result = await graph.tests_for(
        session, repo.id, targets, min_confidence=min_confidence, limit=limit
    )
    return _graph(repo, targets, result)


@router.get("/hierarchy")
async def hierarchy(
    repo_id: int,
    session: Session,
    symbol: SymbolQ = None,
    symbol_id: SymbolIdQ = None,
    limit: Limit = 100,
) -> HierarchyOut:
    """Base classes and subclasses (or implemented and implementing interfaces)."""
    repo = await _searchable(session, repo_id)
    targets = await _targets(session, repo, symbol, symbol_id)
    bases, subclasses, truncated = await graph.hierarchy(
        session, repo.id, targets, limit=limit
    )
    return HierarchyOut(
        targets=[_symbol_out(repo, t) for t in targets],
        bases=[_edge_out(repo, h) for h in bases],
        subclasses=[_edge_out(repo, h) for h in subclasses],
        truncated=truncated,
    )
