from dataclasses import dataclass

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import File, Symbol


@dataclass
class SymbolHit:
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


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def symbol_search(
    session: AsyncSession,
    repo_id: int,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> list[SymbolHit]:
    """Find symbols by name or qualified name, best matches first.

    Ranking: exact name, case-insensitive name, qualified-name suffix,
    name prefix, name substring, qualified-name substring.
    """
    q = query.strip()
    if not q:
        return []
    lq = q.lower()
    contains = f"%{_like_escape(lq)}%"
    lname = func.lower(Symbol.name)
    lqual = func.lower(Symbol.qualified_name)
    rank = case(
        (Symbol.name == q, 0),
        (lname == lq, 1),
        (
            or_(
                lqual == lq,
                lqual.endswith("." + lq, autoescape=True),
                lqual.endswith(":" + lq, autoescape=True),
            ),
            2,
        ),
        (lname.startswith(lq, autoescape=True), 3),
        (lname.like(contains, escape="\\"), 4),
        else_=5,
    )
    stmt = (
        select(Symbol, File.path, File.is_test)
        .join(File, File.id == Symbol.file_id)
        .where(
            Symbol.repo_id == repo_id,
            or_(lname.like(contains, escape="\\"), lqual.like(contains, escape="\\")),
        )
        .order_by(
            rank,
            case((Symbol.kind == "module", 1), else_=0),
            File.is_test,
            func.length(Symbol.name),
            Symbol.qualified_name,
        )
        .limit(limit)
    )
    if kind:
        stmt = stmt.where(Symbol.kind == kind)
    rows = await session.execute(stmt)
    return [
        SymbolHit(
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
        for s, path, is_test in rows
    ]
