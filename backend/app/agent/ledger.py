from pathlib import Path

from app.answer.links import github_url
from app.answer.schema import Evidence
from app.models import Repo

MAX_EVIDENCE_LINES = 300


class Ledger:
    """Every location a tool showed the model, keyed by evidence id. The
    answer may only cite ids from here, and each entry is checked against
    the checkout at the indexed SHA when it is added."""

    def __init__(self, repo: Repo, checkout: Path, prior: list[Evidence] = ()):
        self.repo = repo
        self.checkout = checkout.resolve()
        self.entries: dict[str, Evidence] = {}
        self._by_loc: dict[tuple[str, int, int], str] = {}
        self._lines: dict[str, list[str] | None] = {}
        for e in prior:
            self.entries[e.id] = e
            self._by_loc[(e.path, e.start_line, e.end_line)] = e.id
        self._next = 1 + max(
            (int(i[1:]) for i in self.entries if i[1:].isdigit()), default=0
        )

    def lines(self, path: str) -> list[str] | None:
        """The file's lines, or None when the path is not a file in the checkout."""
        if path not in self._lines:
            target = (self.checkout / path).resolve()
            if not target.is_relative_to(self.checkout) or not target.is_file():
                self._lines[path] = None
            else:
                try:
                    text = target.read_text("utf-8", errors="replace")
                    self._lines[path] = text.splitlines()
                except OSError:
                    self._lines[path] = None
        return self._lines[path]

    def add(
        self,
        path: str,
        start: int,
        end: int | None = None,
        *,
        symbol: str | None = None,
        is_test: bool = False,
        score: float | None = None,
    ) -> str | None:
        """Record a range and return its evidence id, reusing the id of an
        identical range. None when the path or range is not in the checkout."""
        lines = self.lines(path)
        if not lines:
            return None
        end = end if end is not None else start
        start = max(1, min(start, len(lines)))
        end = max(start, min(end, len(lines), start + MAX_EVIDENCE_LINES - 1))
        key = (path, start, end)
        if key in self._by_loc:
            existing = self.entries[self._by_loc[key]]
            if symbol and not existing.symbol:
                existing.symbol = symbol
            return existing.id
        eid = f"e{self._next}"
        self._next += 1
        self.entries[eid] = Evidence(
            id=eid,
            path=path,
            symbol=symbol,
            start_line=start,
            end_line=end,
            url=github_url(self.repo, path, start, end),
            content="\n".join(lines[start - 1 : end]),
            score=score,
            is_test=is_test,
        )
        self._by_loc[key] = eid
        return eid

    def __len__(self) -> int:
        return len(self.entries)
