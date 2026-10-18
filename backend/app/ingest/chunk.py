from dataclasses import dataclass
from pathlib import PurePosixPath

from app.ingest.parse import ParsedFile

MAX_LINES = 120
OVERLAP = 15
MAX_CHARS = 8000
MIN_GAP_LINES = 3
DOC_SUFFIXES = {".md", ".mdx", ".rst"}
DOC_WINDOW = 60

# Symbol kinds whose whole body becomes one chunk (split when long).
_BODY_KINDS = {"function", "method", "interface", "type", "enum"}
_NESTING_KINDS = {"function", "method"}


@dataclass
class ParsedChunk:
    kind: str  # symbol, header, code or doc
    symbol: int | None  # index into ParsedFile.symbols
    start_line: int
    end_line: int
    content: str


def _windows(start: int, end: int, size: int, overlap: int) -> list[tuple[int, int]]:
    if end - start + 1 <= size:
        return [(start, end)]
    out = []
    s = start
    while True:
        e = min(s + size - 1, end)
        out.append((s, e))
        if e == end:
            return out
        s = e - overlap + 1


def _content(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1 : end])[:MAX_CHARS]


def is_doc_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in DOC_SUFFIXES


def chunk_source(src: bytes, parsed: ParsedFile) -> list[ParsedChunk]:
    """Chunk by symbol, then cover the remaining lines (imports, top-level code)."""
    lines = src.decode("utf-8", errors="replace").splitlines()
    n = len(lines)
    if n == 0:
        return []
    symbols = parsed.symbols
    chunks: list[ParsedChunk] = []
    covered = [False] * (n + 2)

    def nested_in_body(i: int) -> bool:
        p = symbols[i].parent
        while p is not None:
            if symbols[p].kind in _NESTING_KINDS:
                return True
            p = symbols[p].parent
        return False

    first_child: dict[int, int] = {}
    for i, s in enumerate(symbols[1:], start=1):
        if s.parent is not None and s.parent not in first_child:
            first_child[s.parent] = s.start_line

    def add(kind: str, symbol: int | None, start: int, end: int) -> None:
        end = min(end, n)
        for ws, we in _windows(start, end, MAX_LINES, OVERLAP):
            chunks.append(ParsedChunk(kind, symbol, ws, we, _content(lines, ws, we)))
        for line in range(start, end + 1):
            covered[line] = True

    for i, s in enumerate(symbols[1:], start=1):
        if nested_in_body(i):
            continue
        if s.kind in _BODY_KINDS:
            add("symbol", i, s.start_line, s.end_line)
        elif s.kind == "class":
            # Methods get their own chunks; the class chunk is its header and fields.
            end = first_child.get(i, s.end_line + 1) - 1
            if end >= s.start_line:
                add("symbol", i, s.start_line, end)

    # Innermost enclosing symbol for each line, for attributing gap chunks.
    owner: list[int | None] = [None] * (n + 2)
    for i, s in enumerate(symbols[1:], start=1):
        for line in range(s.start_line, min(s.end_line, n) + 1):
            owner[line] = i

    line = 1
    while line <= n:
        if covered[line]:
            line += 1
            continue
        start = line
        while line <= n and not covered[line] and owner[line] == owner[start]:
            line += 1
        end = line - 1
        if sum(1 for x in lines[start - 1 : end] if x.strip()) < MIN_GAP_LINES:
            continue
        kind = "header" if start == 1 else "code"
        for ws, we in _windows(start, end, MAX_LINES, OVERLAP):
            chunks.append(
                ParsedChunk(kind, owner[start], ws, we, _content(lines, ws, we))
            )

    chunks.sort(key=lambda c: (c.start_line, c.end_line))
    return chunks


def chunk_doc(src: bytes) -> list[ParsedChunk]:
    lines = src.decode("utf-8", errors="replace").splitlines()
    out = []
    for s, e in _windows(1, len(lines), DOC_WINDOW, 10) if lines else []:
        text = _content(lines, s, e)
        if text.strip():
            out.append(ParsedChunk("doc", None, s, e, text))
    return out
