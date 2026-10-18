import asyncio
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_NAME = r"[A-Za-z0-9_.-]+"
_URL_RE = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>{_NAME})/(?P<name>{_NAME}?)"
    r"(?:\.git)?(?:/.*)?$"
)
_SHORT_RE = re.compile(rf"^(?P<owner>{_NAME})/(?P<name>{_NAME}?)(?:\.git)?$")

# Never prompt for credentials: a private or missing repo must fail fast.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


class IngestError(Exception):
    """An ingest failure with a message that is safe to show to the user."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"


@dataclass(frozen=True)
class RepoMeta:
    default_branch: str | None
    size_kb: int | None


def parse_github_url(raw: str) -> RepoRef:
    text = raw.strip().rstrip("/")
    match = _URL_RE.match(text) or _SHORT_RE.match(text)
    if not match or match["name"] in {"", ".", ".."}:
        raise ValueError(
            "Expected a GitHub repository URL like https://github.com/owner/repo"
        )
    return RepoRef(owner=match["owner"], name=match["name"])


def _fetch_meta(ref: RepoRef) -> RepoMeta:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{ref.owner}/{ref.name}",
        headers={"Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise IngestError(
                f"{ref.owner}/{ref.name} was not found on GitHub, or it is private."
            ) from exc
        # Rate limits and other API hiccups should not block a clone.
        return RepoMeta(default_branch=None, size_kb=None)
    except (urllib.error.URLError, TimeoutError):
        return RepoMeta(default_branch=None, size_kb=None)
    if data.get("private"):
        raise IngestError("Only public repositories are supported.")
    return RepoMeta(default_branch=data.get("default_branch"), size_kb=data.get("size"))


async def fetch_meta(ref: RepoRef) -> RepoMeta:
    return await asyncio.to_thread(_fetch_meta, ref)


async def _git(*args: str, cwd: Path | None = None, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=_GIT_ENV,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise IngestError("Cloning took too long and was stopped.") from None
    if proc.returncode != 0:
        detail = err.decode(errors="replace").strip().splitlines()
        raise IngestError(
            f"git {args[0]} failed: {detail[-1] if detail else 'unknown error'}"
        )
    return out.decode().strip()


def dir_size_mb(path: Path) -> float:
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            p = Path(root, f)
            if not p.is_symlink():
                total += p.stat().st_size
    return total / (1024 * 1024)


async def shallow_clone(ref: RepoRef, dest: Path, timeout: float) -> str:
    """Clone the default branch head into dest and return its commit SHA."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _git(
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        f"{ref.url}.git",
        str(dest),
        timeout=timeout,
    )
    return await _git("rev-parse", "HEAD", cwd=dest, timeout=30)
