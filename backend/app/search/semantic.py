from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.embed import get_embedder
from app.models import Chunk, File, Symbol
from app.search.text import SearchError


class SemanticUnavailable(SearchError):
    """Semantic search cannot run yet (no key or no vectors)."""


@dataclass
class ChunkHit:
    id: int
    kind: str
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    score: float  # cosine similarity, higher is closer
    is_test: bool


async def embedded_count(session: AsyncSession, repo_id: int) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.repo_id == repo_id, Chunk.embedding.is_not(None))
        )
    ).scalar_one()


async def semantic_search(
    session: AsyncSession, repo_id: int, query: str, k: int = 10
) -> list[ChunkHit]:
    embedder = get_embedder()
    if embedder is None:
        raise SemanticUnavailable(
            "Semantic search needs VOYAGE_API_KEY. Add it to .env and restart the backend."
        )
    if await embedded_count(session, repo_id) == 0:
        raise SemanticUnavailable(
            "This repository has no embeddings yet. Run Embed (or Re-index) first."
        )
    vec = await embedder.embed_query(query)
    distance = Chunk.embedding.cosine_distance(vec)
    # Keep scanning the HNSW index past other repos' vectors until k rows match.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = await session.execute(
        select(
            Chunk.id,
            Chunk.kind,
            File.path,
            Symbol.qualified_name,
            Chunk.start_line,
            Chunk.end_line,
            Chunk.content,
            distance,
            File.is_test,
        )
        .join(File, File.id == Chunk.file_id)
        .outerjoin(Symbol, Symbol.id == Chunk.symbol_id)
        .where(Chunk.repo_id == repo_id, Chunk.embedding.is_not(None))
        .order_by(distance)
        .limit(k)
    )
    return [
        ChunkHit(*row[:7], score=round(1 - row[7], 4), is_test=row[8]) for row in rows
    ]
