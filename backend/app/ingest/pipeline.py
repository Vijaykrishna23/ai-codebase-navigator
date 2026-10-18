import asyncio
import logging
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.ingest.github import (
    IngestError,
    RepoRef,
    dir_size_mb,
    fetch_meta,
    shallow_clone,
)
from app.ingest.parse import parse_source
from app.ingest.walk import WalkedFile, walk_repo
from app.models import File, Repo, RepoStatus, Symbol

log = logging.getLogger(__name__)

BATCH_FILES = 200

_tasks: dict[int, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None


def repos_dir() -> Path:
    return get_settings().data_dir / "repos"


def clone_dir(owner: str, name: str, sha: str) -> Path:
    return repos_dir() / f"{owner}__{name}__{sha}"


def start_ingest(repo_id: int) -> None:
    task = asyncio.create_task(_run(repo_id))
    _tasks[repo_id] = task
    task.add_done_callback(lambda _: _tasks.pop(repo_id, None))


async def cancel_ingest(repo_id: int) -> None:
    task = _tasks.get(repo_id)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def mark_interrupted() -> None:
    """Fail ingests a previous server process left half done."""
    async with SessionLocal() as session:
        await session.execute(
            update(Repo)
            .where(Repo.status.in_(RepoStatus.ACTIVE))
            .values(
                status=RepoStatus.FAILED,
                error="Interrupted by a server restart. Ingest it again to retry.",
            )
        )
        await session.commit()
    for tmp in repos_dir().glob(".tmp-*"):
        shutil.rmtree(tmp, ignore_errors=True)


async def _update(repo_id: int, **values: Any) -> None:
    async with SessionLocal() as session:
        await session.execute(update(Repo).where(Repo.id == repo_id).values(**values))
        await session.commit()


async def _run(repo_id: int) -> None:
    global _semaphore
    settings = get_settings()
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent_ingests)
    async with _semaphore:
        try:
            await asyncio.wait_for(_ingest(repo_id), settings.ingest_timeout_s)
        except IngestError as exc:
            await _fail(repo_id, str(exc))
        except TimeoutError:
            minutes = settings.ingest_timeout_s // 60
            await _fail(repo_id, f"Ingest took longer than {minutes} minutes.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Ingest of repo %s failed", repo_id)
            await _fail(repo_id, f"Unexpected error: {exc.__class__.__name__}: {exc}")


async def _fail(repo_id: int, message: str) -> None:
    await _update(repo_id, status=RepoStatus.FAILED, error=message)
    shutil.rmtree(repos_dir() / f".tmp-{repo_id}", ignore_errors=True)


async def _ingest(repo_id: int) -> None:
    settings = get_settings()
    started = time.monotonic()

    async with SessionLocal() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:
            return
        ref = RepoRef(repo.owner, repo.name)
        old_sha = repo.sha
        # Re-ingest starts from a clean index.
        await session.execute(delete(File).where(File.repo_id == repo_id))
        repo.status = RepoStatus.CLONING
        repo.error = None
        repo.stats = {}
        await session.commit()

    meta = await fetch_meta(ref)
    # GitHub reports full history size; a shallow clone is smaller, so only
    # reject far-too-big repos here and check the checkout precisely below.
    if meta.size_kb is not None and meta.size_kb > settings.max_clone_mb * 1024 * 5:
        raise IngestError(
            f"Repository is {meta.size_kb // 1024} MB on GitHub, "
            f"over the {settings.max_clone_mb} MB limit."
        )

    tmp = repos_dir() / f".tmp-{repo_id}"
    sha = await shallow_clone(ref, tmp, timeout=settings.ingest_timeout_s)
    clone_mb = await asyncio.to_thread(dir_size_mb, tmp)
    if clone_mb > settings.max_clone_mb:
        raise IngestError(
            f"Checkout is {clone_mb:.0f} MB, over the {settings.max_clone_mb} MB limit."
        )
    dest = clone_dir(ref.owner, ref.name, sha)
    if old_sha and old_sha != sha:
        shutil.rmtree(clone_dir(ref.owner, ref.name, old_sha), ignore_errors=True)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)

    await _update(
        repo_id,
        status=RepoStatus.PARSING,
        sha=sha,
        default_branch=meta.default_branch,
    )

    files = await asyncio.to_thread(
        lambda: list(walk_repo(dest, settings.max_file_kb * 1024))
    )
    source_files = [f for f in files if f.language]
    if len(source_files) > settings.max_source_files:
        raise IngestError(
            f"Repository has {len(source_files)} Python, JavaScript and TypeScript "
            f"files, over the {settings.max_source_files} file limit."
        )

    stats: dict[str, Any] = {
        "clone_mb": round(clone_mb, 1),
        "files": len(files),
        "source_files": len(source_files),
        "test_files": sum(1 for f in source_files if f.is_test),
        "languages": dict(Counter(f.language for f in source_files).most_common()),
        "parsed": 0,
        "symbols": 0,
        "parse_errors": 0,
    }
    await _update(repo_id, stats=stats)

    for start in range(0, len(files), BATCH_FILES):
        batch = files[start : start + BATCH_FILES]
        parsed = await asyncio.to_thread(_parse_batch, batch)
        n_symbols, n_errors = await _store_batch(repo_id, parsed)
        stats["parsed"] += sum(1 for f in batch if f.language)
        stats["symbols"] += n_symbols
        stats["parse_errors"] += n_errors
        await _update(repo_id, stats=dict(stats))

    stats["duration_s"] = round(time.monotonic() - started, 1)
    await _update(
        repo_id,
        status=RepoStatus.READY,
        stats=stats,
        indexed_at=datetime.now(UTC),
    )


def _parse_batch(batch: list[WalkedFile]) -> list[tuple[WalkedFile, int, Any]]:
    out = []
    for f in batch:
        src = f.abs_path.read_bytes()
        loc = src.count(b"\n") + (0 if src.endswith(b"\n") else 1)
        parsed = parse_source(f.path, f.language, src) if f.language else None
        out.append((f, loc, parsed))
    return out


async def _next_ids(session, table: str, n: int) -> list[int]:
    if n == 0:
        return []
    result = await session.execute(
        text(
            "SELECT nextval(pg_get_serial_sequence(:table, 'id')) "
            "FROM generate_series(1, :n)"
        ),
        {"table": table, "n": n},
    )
    return list(result.scalars())


async def _store_batch(repo_id: int, parsed: list) -> tuple[int, int]:
    async with SessionLocal() as session:
        file_ids = await _next_ids(session, "files", len(parsed))
        file_rows = []
        symbol_rows = []
        n_symbols = sum(len(p.symbols) for _, _, p in parsed if p)
        symbol_ids = iter(await _next_ids(session, "symbols", n_symbols))
        n_errors = 0
        for file_id, (f, loc, p) in zip(file_ids, parsed, strict=True):
            file_rows.append(
                {
                    "id": file_id,
                    "repo_id": repo_id,
                    "path": f.path,
                    "language": f.language,
                    "size_bytes": f.size_bytes,
                    "loc": loc,
                    "is_test": f.is_test,
                    "parse_error": bool(p and p.has_error),
                }
            )
            if p is None:
                continue
            n_errors += p.has_error
            ids = [next(symbol_ids) for _ in p.symbols]
            for sid, s in zip(ids, p.symbols, strict=True):
                symbol_rows.append(
                    {
                        "id": sid,
                        "repo_id": repo_id,
                        "file_id": file_id,
                        "parent_id": ids[s.parent] if s.parent is not None else None,
                        "kind": s.kind,
                        "name": s.name,
                        "qualified_name": s.qualified_name,
                        "start_line": s.start_line,
                        "end_line": s.end_line,
                        "signature": s.signature,
                        "docstring": s.docstring,
                    }
                )
        await session.execute(insert(File), file_rows)
        if symbol_rows:
            await session.execute(insert(Symbol), symbol_rows)
        await session.commit()
    return n_symbols, n_errors


async def delete_repo_data(repo: Repo) -> None:
    await cancel_ingest(repo.id)
    if repo.sha:
        shutil.rmtree(clone_dir(repo.owner, repo.name, repo.sha), ignore_errors=True)
    shutil.rmtree(repos_dir() / f".tmp-{repo.id}", ignore_errors=True)


async def find_repo(session, ref: RepoRef) -> Repo | None:
    result = await session.execute(
        select(Repo).where(
            func.lower(Repo.owner) == ref.owner.lower(),
            func.lower(Repo.name) == ref.name.lower(),
        )
    )
    return result.scalar_one_or_none()
