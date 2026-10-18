import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    "jspm_packages",
    "vendor",
    "third_party",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".cache",
    ".parcel-cache",
    "coverage",
    ".nyc_output",
    ".venv",
    "venv",
    ".env",
    "site-packages",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".eggs",
    ".yarn",
    ".pnpm-store",
    ".idea",
    ".vscode",
}

LOCKFILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "pdm.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
}

# Usually build output, but also legit package names (e.g. pypa/build's src/build).
AMBIGUOUS_DIRS = {"dist", "build", "out", "env", "lib-cov"}

_MINIFIED_RE = re.compile(r"[.-]min\.(js|mjs|cjs|css)$|\.bundle\.js$|\.chunk\.js$")
_TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$|.*_test\.py$|^tests?\.py$|^conftest\.py$"
    r"|.*\.(test|spec)\.(js|jsx|mjs|cjs|ts|tsx|mts|cts)$)"
)
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "__mocks__"}


@dataclass(frozen=True)
class WalkedFile:
    path: str
    abs_path: Path
    language: str | None
    size_bytes: int
    is_test: bool


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(_TEST_FILE_RE.match(p.name)) or any(
        part in _TEST_DIRS for part in p.parts[:-1]
    )


def _is_binary(head: bytes) -> bool:
    return b"\x00" in head


def _looks_minified(head: bytes) -> bool:
    lines = head.splitlines()[:20]
    if not lines:
        return False
    return sum(len(line) for line in lines) / len(lines) > 400


def walk_repo(root: Path, max_file_bytes: int) -> Iterator[WalkedFile]:
    """Yield indexable text files, skipping vendored, generated and binary files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not (
                d in AMBIGUOUS_DIRS and not Path(dirpath, d, "__init__.py").exists()
            )
            and not d.endswith(".egg-info")
            and not Path(dirpath, d).is_symlink()
        )
        for fname in sorted(filenames):
            abs_path = Path(dirpath, fname)
            if (
                abs_path.is_symlink()
                or fname in LOCKFILES
                or _MINIFIED_RE.search(fname)
            ):
                continue
            size = abs_path.stat().st_size
            if size == 0 or size > max_file_bytes:
                continue
            with abs_path.open("rb") as fh:
                head = fh.read(8192)
            if _is_binary(head):
                continue
            language = LANGUAGES.get(abs_path.suffix.lower())
            if language and language != "python" and _looks_minified(head):
                continue
            rel = abs_path.relative_to(root).as_posix()
            yield WalkedFile(
                path=rel,
                abs_path=abs_path,
                language=language,
                size_bytes=size,
                is_test=is_test_path(rel),
            )
