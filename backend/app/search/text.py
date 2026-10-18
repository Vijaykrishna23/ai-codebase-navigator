import asyncio
import json
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.ingest.walk import LOCKFILES, SKIP_DIRS

MAX_LINE_CHARS = 400
MAX_PER_FILE = 20
TIMEOUT_S = 15

_RG_FALLBACKS = ("/opt/homebrew/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg")
_EXCLUDE_GLOBS = [
    *(f"!{d}" for d in sorted(SKIP_DIRS)),
    *(f"!{f}" for f in sorted(LOCKFILES)),
    "!*.min.js",
    "!*.min.css",
    "!*.map",
]


class SearchError(Exception):
    """A search failure with a message that is safe to show to the user."""


@dataclass
class TextMatch:
    path: str
    line: int
    text: str
    # Character offsets of each match within text.
    ranges: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class TextSearchResult:
    matches: list[TextMatch]
    truncated: bool


@lru_cache
def rg_path() -> str | None:
    configured = get_settings().rg_path
    if configured:
        return configured
    # IDE-launched servers often lack Homebrew on PATH.
    return shutil.which("rg") or next(
        (p for p in _RG_FALLBACKS if Path(p).is_file()), None
    )


def _char_ranges(line: str, submatches: list[dict]) -> list[tuple[int, int]]:
    raw = line.encode("utf-8")
    out = []
    for m in submatches:
        start = len(raw[: m["start"]].decode("utf-8", errors="ignore"))
        end = len(raw[: m["end"]].decode("utf-8", errors="ignore"))
        if start < MAX_LINE_CHARS:
            out.append((start, min(end, MAX_LINE_CHARS)))
    return out


async def text_search(
    root: Path,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool | None = None,
    path_glob: str | None = None,
    limit: int = 100,
) -> TextSearchResult:
    """Search a checkout with ripgrep. case_sensitive=None means smart case."""
    rg = rg_path()
    if rg is None:
        raise SearchError("ripgrep is not installed. Run: brew install ripgrep")
    if not pattern:
        raise SearchError("Search pattern is empty.")
    if not root.is_dir():
        raise SearchError("The repository checkout is missing. Re-index it.")

    args = [
        rg,
        "--json",
        "--no-config",
        "--sort",
        "path",
        "--max-count",
        str(MAX_PER_FILE),
        "--max-filesize",
        f"{get_settings().max_file_kb}K",
    ]
    if not regex:
        args.append("--fixed-strings")
    if case_sensitive is None:
        args.append("--smart-case")
    elif not case_sensitive:
        args.append("--ignore-case")
    for glob in _EXCLUDE_GLOBS:
        args += ["--glob", glob]
    if path_glob:
        args += ["--glob", path_glob]
    # An explicit path, or rg reads stdin when it is not a terminal.
    args += ["--regexp", pattern, "--", "."]

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=root,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    matches: list[TextMatch] = []
    truncated = False

    async def read() -> None:
        nonlocal truncated
        assert proc.stdout is not None
        async for raw in proc.stdout:
            event = json.loads(raw)
            if event["type"] != "match":
                continue
            data = event["data"]
            line = data["lines"].get("text")
            path = data["path"].get("text")
            if line is None or path is None:
                continue  # not valid UTF-8
            line = line.rstrip("\n")
            matches.append(
                TextMatch(
                    path=path.removeprefix("./"),
                    line=data["line_number"],
                    text=line[:MAX_LINE_CHARS],
                    ranges=_char_ranges(line, data["submatches"]),
                )
            )
            if len(matches) >= limit:
                truncated = True
                proc.kill()
                return

    try:
        await asyncio.wait_for(read(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        truncated = True
    stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
    code = await proc.wait()
    # 0 = matches, 1 = no matches, 2 = error (bad regex, unreadable file).
    if code == 2 and not matches:
        raise SearchError(
            stderr.strip().splitlines()[-1] if stderr.strip() else "ripgrep failed"
        )
    matches.sort(key=lambda m: (m.path, m.line))
    return TextSearchResult(matches=matches, truncated=truncated)
