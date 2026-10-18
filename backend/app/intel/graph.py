"""Queries over the code-intelligence edges: definitions, callers, callees,
references, imports, importers, tests and class hierarchy."""

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Edge, EdgeKind, File, Symbol
from app.search.symbols import SymbolHit, symbol_search
from app.search.text import SearchError, text_search

MAX_TARGETS = 5
MAX_SOURCE_LINES = 300
CONFIDENCE_LEVELS = {
    "high": ("high",),
    "medium": ("high", "medium"),
    "low": ("high", "medium", "low"),
}


@dataclass
class End:
    """One side of an edge: a symbol, a whole file, or an external name."""

    path: str | None
    name: str
    symbol_id: int | None = None
    kind: str | None = None
    qualified_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    is_test: bool = False

    @property
    def external(self) -> bool:
        return self.path is None


@dataclass
class EdgeHit:
    kind: str
    confidence: str
    src: End
    dst: End
    line: int | None  # where it happens, in the source file
    label: str | None = None  # test title, "implements", "file name"
    text: str | None = None  # the matching line, for text matches


@dataclass
class Definition:
    symbol: SymbolHit
    parent: str | None
    source: str
    truncated: bool


async def find_symbols(
    session: AsyncSession,
    repo_id: int,
    symbol: str | None = None,
    symbol_id: int | None = None,
) -> list[SymbolHit]:
    """Exact matches for a name or qualified name, or the symbol with that id."""
    if symbol_id is not None:
        row = (
            await session.execute(
                select(Symbol, File.path, File.is_test)
                .join(File, File.id == Symbol.file_id)
                .where(Symbol.repo_id == repo_id, Symbol.id == symbol_id)
            )
        ).first()
        if row is None:
            return []
        s, path, is_test = row
        return [_hit(s, path, is_test)]
    q = (symbol or "").strip()
    if not q:
        return []
    hits = await symbol_search(session, repo_id, q, limit=MAX_TARGETS * 8)
    lq = q.lower()

    def exact(h: SymbolHit) -> bool:
        qual = h.qualified_name.lower()
        return h.name.lower() == lq or qual == lq or qual.endswith(("." + lq, ":" + lq))

    matches = [h for h in hits if exact(h)]
    same_case = [h for h in matches if h.name == q or h.qualified_name.endswith(q)]
    # Overloads share a qualified name; keep the last one, the implementation,
    # which is also what the resolver links calls to.
    by_key: dict[tuple[str, str], SymbolHit] = {}
    for h in same_case or matches:
        key = (h.path, h.qualified_name)
        if key not in by_key or h.start_line > by_key[key].start_line:
            by_key[key] = h
    return list(by_key.values())[:MAX_TARGETS]


def _hit(s: Symbol, path: str, is_test: bool) -> SymbolHit:
    return SymbolHit(
        id=s.id,
        kind=s.kind,
        name=s.name,
        qualified_name=s.qualified_name,
        path=path,
        start_line=s.start_line,
        end_line=s.end_line,
        signature=s.signature,
        docstring=s.docstring.split("\n\n", 1)[0] if s.docstring else None,
        is_test=is_test,
    )


async def file_by_path(session: AsyncSession, repo_id: int, path: str) -> File | None:
    path = path.strip().strip("/")
    return (
        await session.execute(
            select(File).where(File.repo_id == repo_id, File.path == path)
        )
    ).scalar_one_or_none()


async def definitions(
    session: AsyncSession, checkout: Path, targets: list[SymbolHit]
) -> list[Definition]:
    out = []
    for t in targets:
        parent = (
            await session.execute(
                select(Symbol.qualified_name).where(
                    Symbol.id
                    == select(Symbol.parent_id)
                    .where(Symbol.id == t.id)
                    .scalar_subquery()
                )
            )
        ).scalar_one_or_none()
        try:
            lines = (
                (checkout / t.path).read_text("utf-8", errors="replace").splitlines()
            )
        except OSError:
            lines = []
        end = min(t.end_line, t.start_line + MAX_SOURCE_LINES - 1)
        out.append(
            Definition(
                symbol=t,
                parent=parent,
                source="\n".join(lines[t.start_line - 1 : end]),
                truncated=end < t.end_line,
            )
        )
    return out


async def query_edges(
    session: AsyncSession,
    repo_id: int,
    kinds: tuple[str, ...],
    *conditions,
    min_confidence: str = "low",
    limit: int = 100,
) -> tuple[list[EdgeHit], bool]:
    src_sym = aliased(Symbol)
    dst_sym = aliased(Symbol)
    src_file = aliased(File)
    dst_file = aliased(File)
    rank = case(
        (Edge.confidence == "high", 0), (Edge.confidence == "medium", 1), else_=2
    )
    stmt = (
        select(Edge, src_sym, src_file, dst_sym, dst_file)
        .join(src_file, src_file.id == Edge.src_file_id)
        .outerjoin(src_sym, src_sym.id == Edge.src_symbol_id)
        .outerjoin(dst_sym, dst_sym.id == Edge.dst_symbol_id)
        .outerjoin(dst_file, dst_file.id == Edge.dst_file_id)
        .where(
            Edge.repo_id == repo_id,
            Edge.kind.in_(kinds),
            Edge.confidence.in_(CONFIDENCE_LEVELS[min_confidence]),
            *conditions,
        )
        .order_by(rank, src_file.is_test, src_file.path, Edge.line, Edge.id)
        .limit(limit + 1)
    )
    rows = (await session.execute(stmt)).all()
    hits = [
        EdgeHit(
            kind=e.kind,
            confidence=e.confidence,
            src=_end(ss, sf, e.src_name or sf.path),
            dst=_end(ds, df, e.dst_name),
            line=e.line,
            label=e.src_name,
        )
        for e, ss, sf, ds, df in rows[:limit]
    ]
    return hits, len(rows) > limit


def _end(s: Symbol | None, f: File | None, name: str) -> End:
    if s is not None:
        return End(
            path=f.path if f is not None else None,
            name=s.name,
            symbol_id=s.id,
            kind=s.kind,
            qualified_name=s.qualified_name,
            start_line=s.start_line,
            end_line=s.end_line,
            is_test=bool(f and f.is_test),
        )
    if f is not None:
        return End(path=f.path, name=f.path, kind="file", is_test=f.is_test)
    return End(path=None, name=name)


def _ids(targets: list[SymbolHit]) -> list[int]:
    return [t.id for t in targets]


async def callers(session, repo_id, targets, **kw) -> tuple[list[EdgeHit], bool]:
    return await query_edges(
        session,
        repo_id,
        (EdgeKind.CALLS,),
        Edge.dst_symbol_id.in_(_ids(targets)),
        **kw,
    )


async def callees(session, repo_id, targets, **kw) -> tuple[list[EdgeHit], bool]:
    return await query_edges(
        session,
        repo_id,
        (EdgeKind.CALLS,),
        Edge.src_symbol_id.in_(_ids(targets)),
        **kw,
    )


async def references(
    session,
    repo_id: int,
    checkout: Path,
    targets: list[SymbolHit],
    *,
    text_fallback: bool = True,
    **kw,
) -> tuple[list[EdgeHit], bool]:
    """Resolved calls, references, imports and inheritance pointing at the
    targets, then plain identifier matches not explained by an edge (low)."""
    hits, truncated = await query_edges(
        session,
        repo_id,
        (EdgeKind.CALLS, EdgeKind.REFERENCES, EdgeKind.IMPORTS, EdgeKind.INHERITS),
        Edge.dst_symbol_id.in_(_ids(targets)),
        **kw,
    )
    if not text_fallback or truncated or kw.get("min_confidence", "low") != "low":
        return hits, truncated
    limit = kw.get("limit", 100)
    covered = {(h.src.path, h.line) for h in hits}
    covered |= {(t.path, t.start_line) for t in targets}
    for name in sorted({t.name for t in targets}):
        if not re.fullmatch(r"[\w$]+", name):
            continue
        definition = re.compile(rf"\b(def|class|function)\s+{re.escape(name)}\b")
        try:
            result = await text_search(
                checkout,
                rf"\b{re.escape(name)}\b",
                regex=True,
                case_sensitive=True,
                limit=limit,
            )
        except SearchError:
            continue
        target = targets[0] if len(targets) == 1 else None
        for m in result.matches:
            if (m.path, m.line) in covered or definition.search(m.text):
                continue
            covered.add((m.path, m.line))
            dst = (
                End(
                    path=target.path,
                    name=target.name,
                    symbol_id=target.id,
                    kind=target.kind,
                    qualified_name=target.qualified_name,
                    start_line=target.start_line,
                    end_line=target.end_line,
                    is_test=target.is_test,
                )
                if target is not None
                else End(path=None, name=name)
            )
            hits.append(
                EdgeHit(
                    kind="text",
                    confidence="low",
                    src=End(path=m.path, name=m.path, kind="file"),
                    dst=dst,
                    line=m.line,
                    text=m.text,
                )
            )
            if len(hits) >= limit:
                return hits, True
        truncated = truncated or result.truncated
    return hits, truncated


async def imports_of(session, repo_id, file: File, **kw) -> tuple[list[EdgeHit], bool]:
    return await query_edges(
        session, repo_id, (EdgeKind.IMPORTS,), Edge.src_file_id == file.id, **kw
    )


async def importers_of(
    session, repo_id, file: File, **kw
) -> tuple[list[EdgeHit], bool]:
    return await query_edges(
        session, repo_id, (EdgeKind.IMPORTS,), Edge.dst_file_id == file.id, **kw
    )


async def tests_for(
    session,
    repo_id,
    targets: list[SymbolHit] | None = None,
    file: File | None = None,
    **kw,
) -> tuple[list[EdgeHit], bool]:
    if file is not None:
        condition = Edge.dst_file_id == file.id
    else:
        condition = Edge.dst_symbol_id.in_(_ids(targets or []))
    return await query_edges(session, repo_id, (EdgeKind.TESTS,), condition, **kw)


async def hierarchy(
    session, repo_id, targets: list[SymbolHit], **kw
) -> tuple[list[EdgeHit], list[EdgeHit], bool]:
    bases, t1 = await query_edges(
        session,
        repo_id,
        (EdgeKind.INHERITS,),
        Edge.src_symbol_id.in_(_ids(targets)),
        **kw,
    )
    subclasses, t2 = await query_edges(
        session,
        repo_id,
        (EdgeKind.INHERITS,),
        Edge.dst_symbol_id.in_(_ids(targets)),
        **kw,
    )
    return bases, subclasses, t1 or t2
