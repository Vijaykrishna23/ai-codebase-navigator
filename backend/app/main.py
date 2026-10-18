from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, repos, search
from app.core.config import get_settings
from app.ingest.pipeline import mark_interrupted


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await mark_interrupted()
    yield


app = FastAPI(title="AI Codebase Navigator", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(repos.router, prefix="/api")
app.include_router(search.router, prefix="/api")
