import asyncio
import logging
import shutil
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.ingest.chunk import ParsedChunk, chunk_doc, chunk_source, is_doc_path
from app.ingest.embed import embed_repo, get_embedder
from app.ingest.github import (
    IngestError,
    RepoRef,
    dir_size_mb,
    fetch_meta,
    shallow_clone,
)
from app.ingest.parse import ParsedFile, parse_source
from app.ingest.resolve import LinkFile, build_edges
from app.ingest.walk import WalkedFile, walk_repo
from app.models import Chunk, Edge, File, Repo, RepoStatus, Symbol

log = logging.getLogger(__name__)

BATCH_FILES = 200
BATCH_EDGES = 5000

_tasks: dict[int, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None


def repos_dir() -> Path:
    return get_settings().data_dir / "repos"


def clone_dir(owner: str, name: str, sha: str) -> Path:
    return repos_dir() / f"{owner}__{name}__{sha}"


def start_ingest(repo_id: int) -> None:
    _start(repo_id, _ingest)


def start_link(repo_id: int) -> None:
    """Rebuild the code-intelligence edges of an indexed repo from its checkout."""
    _start(repo_id, _relink)


def start_embed(repo_id: int) -> None:
    """Embed the chunks of an indexed repo that have no vector yet."""
    _start(repo_id, _embed_only)


def _start(repo_id: int, job: Callable[[int], Awaitable[None]]) -> None:
    task = asyncio.create_task(_run(repo_id, job))
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


async def _run(repo_id: int, job: Callable[[int], Awaitable[None]]) -> None:
    global _semaphore
    settings = get_settings()
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.max_concurrent_ingests)
    async with _semaphore:
        try:
            await asyncio.wait_for(job(repo_id), settings.ingest_timeout_s)
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
        "chunks": 0,
        "embedded": 0,
    }
    await _update(repo_id, stats=stats)

    links: list[LinkFile] = []
    for start in range(0, len(files), BATCH_FILES):
        batch = files[start : start + BATCH_FILES]
        parsed = await asyncio.to_thread(_parse_batch, batch)
        n_symbols, n_errors, n_chunks = await _store_batch(repo_id, parsed, links)
        stats["parsed"] += sum(1 for f in batch if f.language)
        stats["symbols"] += n_symbols
        stats["parse_errors"] += n_errors
        stats["chunks"] += n_chunks
        await _update(repo_id, stats=dict(stats))

    await _link_stage(repo_id, dest, links, [f.path for f in files], stats)
    await _embed_stage(repo_id, stats)

    stats["duration_s"] = round(time.monotonic() - started, 1)
    await _update(
        repo_id,
        status=RepoStatus.READY,
        error=None,
        stats=stats,
        indexed_at=datetime.now(UTC),
    )


Parsed = tuple[WalkedFile, int, ParsedFile | None, list[ParsedChunk]]


def _parse_batch(batch: list[WalkedFile]) -> list[Parsed]:
    out = []
    for f in batch:
        src = f.abs_path.read_bytes()
        loc = src.count(b"\n") + (0 if src.endswith(b"\n") else 1)
        parsed = parse_source(f.path, f.language, src) if f.language else None
        if parsed is not None:
            chunks = chunk_source(src, parsed)
        elif is_doc_path(f.path):
            chunks = chunk_doc(src)
        else:
            chunks = []
        out.append((f, loc, parsed, chunks))
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


async def _store_batch(
    repo_id: int, parsed: list[Parsed], links: list[LinkFile]
) -> tuple[int, int, int]:
    async with SessionLocal() as session:
        file_ids = await _next_ids(session, "files", len(parsed))
        file_rows = []
        symbol_rows = []
        chunk_rows = []
        n_symbols = sum(len(p.symbols) for _, _, p, _ in parsed if p)
        symbol_ids = iter(await _next_ids(session, "symbols", n_symbols))
        n_errors = 0
        for file_id, (f, loc, p, chunks) in zip(file_ids, parsed, strict=True):
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
            ids = [next(symbol_ids) for _ in p.symbols] if p else []
            chunk_rows.extend(
                {
                    "repo_id": repo_id,
                    "file_id": file_id,
                    "symbol_id": ids[c.symbol] if c.symbol is not None else None,
                    "kind": c.kind,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "content": c.content,
                }
                for c in chunks
            )
            if p is None:
                continue
            links.append(LinkFile(file_id, f.path, f.language, f.is_test, ids, p))
            n_errors += p.has_error
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
        if chunk_rows:
            await session.execute(insert(Chunk), chunk_rows)
        await session.commit()
    return n_symbols, n_errors, len(chunk_rows)


async def _link_stage(
    repo_id: int,
    checkout: Path,
    links: list[LinkFile],
    all_paths: list[str],
    stats: dict[str, Any],
) -> None:
    """Resolve imports, calls, references, inheritance and tests into edges.
    Never fails the ingest: search works without edges and relink can retry."""
    stats.update(linking="running", linking_note=None)
    await _update(repo_id, stats=dict(stats))
    started = time.monotonic()
    try:
        rows = await asyncio.to_thread(build_edges, repo_id, links, checkout, all_paths)
        async with SessionLocal() as session:
            await session.execute(delete(Edge).where(Edge.repo_id == repo_id))
            for start in range(0, len(rows), BATCH_EDGES):
                await session.execute(insert(Edge), rows[start : start + BATCH_EDGES])
            await session.commit()
        stats["edges"] = len(rows)
        stats["edge_kinds"] = dict(Counter(r["kind"] for r in rows).most_common())
        stats["linking"] = "done"
    except Exception as exc:
        log.exception("Linking repo %s failed", repo_id)
        stats["linking"] = "failed"
        stats["linking_note"] = f"{exc.__class__.__name__}: {exc}"
    stats["linking_duration_s"] = round(time.monotonic() - started, 1)
    await _update(repo_id, stats=dict(stats))


async def _relink(repo_id: int) -> None:
    async with SessionLocal() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None or not repo.sha:
            return
        stats = dict(repo.stats)
        checkout = clone_dir(repo.owner, repo.name, repo.sha)
        files = (
            await session.execute(
                select(File.id, File.path, File.language, File.is_test).where(
                    File.repo_id == repo_id
                )
            )
        ).all()
        symbols = (
            await session.execute(
                select(
                    Symbol.id,
                    Symbol.file_id,
                    Symbol.kind,
                    Symbol.qualified_name,
                    Symbol.start_line,
                ).where(Symbol.repo_id == repo_id)
            )
        ).all()
    if not checkout.is_dir():
        stats["linking"] = "failed"
        stats["linking_note"] = "The checkout is missing. Re-index the repository."
        await _update(repo_id, stats=stats)
        return
    by_file: dict[int, dict[tuple, int]] = {}
    for sid, file_id, kind, qualified_name, start_line in symbols:
        by_file.setdefault(file_id, {}).setdefault(
            (kind, qualified_name, start_line), sid
        )
    sources = [f for f in files if f.language]
    links = await asyncio.to_thread(_reparse, checkout, sources, by_file)
    await _link_stage(repo_id, checkout, links, [f.path for f in files], stats)


def _reparse(
    checkout: Path, files: list, by_file: dict[int, dict[tuple, int]]
) -> list[LinkFile]:
    """Parse the checkout again and match symbols to their stored rows. Symbols
    the stored index does not have (the parser changed since) get no edges."""
    links = []
    for f in files:
        try:
            src = (checkout / f.path).read_bytes()
        except OSError:
            continue
        p = parse_source(f.path, f.language, src)
        stored = by_file.get(f.id, {})
        ids = [stored.get((s.kind, s.qualified_name, s.start_line)) for s in p.symbols]
        links.append(LinkFile(f.id, f.path, f.language, f.is_test, ids, p))
    return links


async def _embed_stage(repo_id: int, stats: dict[str, Any]) -> None:
    """Embed missing chunk vectors. Never fails the ingest: search without
    embeddings still works, and embedding can be retried later."""
    embedder = get_embedder()
    if embedder is None:
        stats["embedding"] = "skipped"
        stats["embedding_note"] = (
            "VOYAGE_API_KEY is not set. Add it to .env, restart the backend, "
            "then run Embed."
        )
        return

    async with SessionLocal() as session:
        base = (
            await session.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.repo_id == repo_id, Chunk.embedding.is_not(None))
            )
        ).scalar_one()
    stats.update(
        embedding="running",
        embedding_model=embedder.model,
        embedding_note=None,
        embedded=base,
    )
    await _update(repo_id, status=RepoStatus.EMBEDDING, stats=dict(stats))
    tokens_before = stats.get("embedding_tokens", 0)

    async def progress(done: int, tokens: int) -> None:
        stats["embedded"] = base + done
        stats["embedding_tokens"] = tokens_before + tokens
        await _update(repo_id, stats=dict(stats))

    try:
        await embed_repo(repo_id, embedder, progress)
        stats["embedding"] = "done"
    except Exception as exc:
        log.exception("Embedding repo %s failed", repo_id)
        stats["embedding"] = "failed"
        stats["embedding_note"] = f"{exc.__class__.__name__}: {exc}"


async def _embed_only(repo_id: int) -> None:
    async with SessionLocal() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:
            return
        stats = dict(repo.stats)
    started = time.monotonic()
    await _embed_stage(repo_id, stats)
    stats["embedding_duration_s"] = round(time.monotonic() - started, 1)
    await _update(repo_id, status=RepoStatus.READY, stats=stats)


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
