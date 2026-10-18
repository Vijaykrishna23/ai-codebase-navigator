from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.ingest.github import parse_github_url
from app.ingest.pipeline import delete_repo_data, find_repo, start_ingest
from app.models import Repo, RepoStatus

router = APIRouter(prefix="/repos", tags=["repos"])

Session = Annotated[AsyncSession, Depends(get_session)]


class RepoCreate(BaseModel):
    url: str


class RepoOut(BaseModel):
    id: int
    owner: str
    name: str
    url: str
    default_branch: str | None
    sha: str | None
    status: str
    error: str | None
    stats: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None

    @classmethod
    def of(cls, repo: Repo) -> "RepoOut":
        return cls.model_validate(repo, from_attributes=True)


async def _get(session: AsyncSession, repo_id: int) -> Repo:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    return repo


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_repo(
    body: RepoCreate, session: Session, response: Response
) -> RepoOut:
    """Queue a repo for ingest, or return it if it is already indexed or in progress."""
    try:
        ref = parse_github_url(body.url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    repo = await find_repo(session, ref)
    if repo is not None and repo.status != RepoStatus.FAILED:
        response.status_code = status.HTTP_200_OK
        return RepoOut.of(repo)

    if repo is None:
        repo = Repo(owner=ref.owner, name=ref.name, url=ref.url)
        session.add(repo)
    repo.status = RepoStatus.QUEUED
    repo.error = None
    repo.stats = {}
    await session.commit()
    await session.refresh(repo)
    start_ingest(repo.id)
    return RepoOut.of(repo)


@router.get("")
async def list_repos(session: Session) -> list[RepoOut]:
    result = await session.execute(select(Repo).order_by(Repo.created_at.desc()))
    return [RepoOut.of(r) for r in result.scalars()]


@router.get("/{repo_id}")
async def get_repo(repo_id: int, session: Session) -> RepoOut:
    return RepoOut.of(await _get(session, repo_id))


@router.post("/{repo_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_repo(repo_id: int, session: Session) -> RepoOut:
    """Clone the latest default branch head and rebuild the index."""
    repo = await _get(session, repo_id)
    if repo.status in RepoStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ingest already in progress")
    repo.status = RepoStatus.QUEUED
    repo.error = None
    await session.commit()
    await session.refresh(repo)
    start_ingest(repo.id)
    return RepoOut.of(repo)


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repo(repo_id: int, session: Session) -> None:
    repo = await _get(session, repo_id)
    await delete_repo_data(repo)
    await session.delete(repo)
    await session.commit()
