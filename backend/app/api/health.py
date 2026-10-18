from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.search.text import rg_path

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict:
    settings = get_settings()
    try:
        await session.execute(text("SELECT 1"))
        db = "ok"
        pgvector = (
            await session.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one_or_none()
    except (SQLAlchemyError, OSError) as exc:
        db = f"error: {exc.__class__.__name__}"
        pgvector = None

    return {
        "status": "ok" if db == "ok" else "degraded",
        "database": db,
        "pgvector": pgvector,
        "llm_model": settings.llm_model,
        "llm_key_set": bool(settings.anthropic_api_key),
        "embedding_model": settings.embedding_model,
        "embedding_key_set": bool(settings.voyage_api_key),
        "ripgrep": rg_path() is not None,
    }
