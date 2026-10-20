import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import voyageai
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import Chunk, File, Symbol

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 24_000
# Voyage caps a request at 1,000 texts and 120K tokens for code models;
# code runs about 3 chars per token, so stay well under both.
BATCH_TEXTS = 128
BATCH_CHARS = 240_000
PAGE_ROWS = 1000


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], int]:
        """Return one vector per text and the tokens billed."""
        ...

    async def embed_query(self, text: str) -> list[float]: ...


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str, dim: int):
        self.model = model
        self.dim = dim
        self._client = voyageai.AsyncClient(api_key=api_key, max_retries=3)

    async def _embed(self, texts: list[str], input_type: str):
        return await self._client.embed(
            texts,
            model=self.model,
            input_type=input_type,
            output_dimension=self.dim,
            truncation=True,
        )

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], int]:
        result = await self._embed(texts, "document")
        return result.embeddings, result.total_tokens

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query")).embeddings[0]


_embedder: Embedder | None = None


def get_embedder() -> Embedder | None:
    """The configured embedder, or None when no API key is set."""
    global _embedder
    settings = get_settings()
    if not settings.voyage_api_key:
        return None
    if _embedder is None:
        _embedder = VoyageEmbedder(
            settings.voyage_api_key, settings.embedding_model, settings.embedding_dim
        )
    return _embedder


def chunk_text(path: str, qualified_name: str | None, content: str) -> str:
    head = f"{path}\n{qualified_name}" if qualified_name else path
    return f"{head}\n\n{content}"[:MAX_TEXT_CHARS]


def _batches(rows: list) -> list[list]:
    out: list[list] = [[]]
    size = 0
    for row in rows:
        n = len(row[1])
        if out[-1] and (len(out[-1]) >= BATCH_TEXTS or size + n > BATCH_CHARS):
            out.append([])
            size = 0
        out[-1].append(row)
        size += n
    return [b for b in out if b]


async def embed_repo(
    repo_id: int,
    embedder: Embedder,
    on_progress: Callable[[int, int], Awaitable[None]],
) -> tuple[int, int]:
    """Embed every chunk of the repo that has no vector yet.

    Calls on_progress(embedded, tokens) after each batch and returns the totals.
    """
    embedded = tokens = 0
    last_id = 0
    while True:
        async with SessionLocal() as session:
            result = await session.execute(
                select(Chunk.id, File.path, Symbol.qualified_name, Chunk.content)
                .join(File, File.id == Chunk.file_id)
                .outerjoin(Symbol, Symbol.id == Chunk.symbol_id)
                .where(
                    Chunk.repo_id == repo_id,
                    Chunk.embedding.is_(None),
                    Chunk.id > last_id,
                )
                .order_by(Chunk.id)
                .limit(PAGE_ROWS)
            )
            rows = [(cid, chunk_text(p, q, c)) for cid, p, q, c in result]
        if not rows:
            return embedded, tokens
        last_id = rows[-1][0]
        for batch in _batches(rows):
            vectors, used = await embedder.embed_documents([t for _, t in batch])
            async with SessionLocal() as session:
                await session.execute(
                    update(Chunk),
                    [
                        {"id": cid, "embedding": vec}
                        for (cid, _), vec in zip(batch, vectors, strict=True)
                    ],
                )
                await session.commit()
            embedded += len(batch)
            tokens += used
            log.debug("Embedded batch of %d chunks, %d so far", len(batch), embedded)
            await on_progress(embedded, tokens)
