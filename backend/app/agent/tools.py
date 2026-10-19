"""The Navigator's tools: thin wrappers over search and code intelligence
that return compact JSON and record every location in the evidence ledger."""

import json
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ledger import Ledger
from app.ingest.walk import SKIP_DIRS
from app.intel import graph
from app.intel.graph import EdgeHit
from app.models import File, Repo
from app.search.semantic import semantic_search
from app.search.symbols import SymbolHit, symbol_search
from app.search.text import SearchError, text_search

MAX_RESULT_CHARS = 16_000
MAX_CODE_CHARS = 200
MAX_READ_LINES = 200
MAX_DEFINITION_LINES = 150
MAX_CHUNK_LINES = 80
README_LINES = 120
ENTRY_POINT_RE = re.compile(
    r"(^|/)(__main__|main|app|manage|cli|server|wsgi|asgi|index)\.(py|js|mjs|cjs|ts|tsx|jsx)$"
)

SYMBOL_ARG = {
    "type": "string",
    "description": "Symbol name or qualified name (e.g. 'validate_token', "
    "'AuthMiddleware.authenticate'), or 'id:123' for an id from an earlier result.",
}
CONFIDENCE_ARG = {
    "type": "string",
    "enum": ["high", "medium", "low"],
    "description": "Lowest edge confidence to include. Default low.",
}


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict] = [
    _tool(
        "repo_overview",
        "README excerpt, languages, top-level layout, counts and likely entry "
        "points. Use first for broad questions about what the repo does.",
        {},
        [],
    ),
    _tool(
        "list_dir",
        "List a directory in the checkout with source file sizes.",
        {"path": {"type": "string", "description": "Directory path, '' for root."}},
        ["path"],
    ),
    _tool(
        "text_search",
        "Exact or regex search over the checkout (ripgrep). Best for literal "
        "strings, config keys, error messages and identifiers.",
        {
            "pattern": {"type": "string"},
            "regex": {"type": "boolean", "description": "Default false."},
            "path_glob": {
                "type": "string",
                "description": "Restrict files, e.g. 'src/**/*.ts' or '!tests/**'.",
            },
        },
        ["pattern"],
    ),
    _tool(
        "symbol_search",
        "Find functions, classes, methods and types by partial name.",
        {
            "name": {"type": "string"},
            "kind": {
                "type": "string",
                "description": "Optional: function, method, class, interface, type, module.",
            },
        },
        ["name"],
    ),
    _tool(
        "semantic_search",
        "Embedding search over code chunks. Best for concepts when you do not "
        "know the names involved.",
        {
            "query": {"type": "string"},
            "k": {"type": "integer", "description": "Results, 1 to 15. Default 8."},
        },
        ["query"],
    ),
    _tool(
        "get_definition",
        "Where a symbol is defined, with its numbered source.",
        {"symbol": SYMBOL_ARG},
        ["symbol"],
    ),
    _tool(
        "find_references",
        "Every usage of a symbol (calls, imports, type uses, inheritance), plus "
        "plain identifier matches at low confidence, grouped by file.",
        {"symbol": SYMBOL_ARG, "min_confidence": CONFIDENCE_ARG},
        ["symbol"],
    ),
    _tool(
        "get_callers",
        "Functions and methods that call the symbol, with the call site.",
        {"symbol": SYMBOL_ARG, "min_confidence": CONFIDENCE_ARG},
        ["symbol"],
    ),
    _tool(
        "get_callees",
        "What the symbol calls, with where each callee is defined. External "
        "package calls have no location.",
        {"symbol": SYMBOL_ARG, "min_confidence": CONFIDENCE_ARG},
        ["symbol"],
    ),
    _tool(
        "get_imports",
        "Modules a file imports and what each resolves to.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _tool(
        "get_importers",
        "Files that import the file or names defined in it.",
        {"path": {"type": "string"}},
        ["path"],
    ),
    _tool(
        "find_tests",
        "Tests that exercise a symbol or a file. Pass exactly one of symbol or path.",
        {"symbol": SYMBOL_ARG, "path": {"type": "string"}},
        [],
    ),
    _tool(
        "get_hierarchy",
        "Base classes and subclasses (or implemented interfaces) of a class.",
        {"symbol": SYMBOL_ARG},
        ["symbol"],
    ),
    _tool(
        "read_code",
        f"Read numbered source lines from a file, up to {MAX_READ_LINES} at a time.",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
        },
        ["path", "start_line", "end_line"],
    ),
]

_REF = {
    "type": "object",
    "properties": {
        "evidence": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "note": {"type": "string"},
    },
    "required": ["evidence", "start_line", "end_line", "note"],
    "additionalProperties": False,
}

SUBMIT_ANSWER = {
    "name": "submit_answer",
    "description": "Submit the final answer. Call it exactly once, when you "
    "have enough evidence or the budget runs out.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "explanation": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "kind": {"type": "string", "enum": ["fact", "interpretation"]},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["text", "kind", "evidence"],
                    "additionalProperties": False,
                },
            },
            "flow": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "integer"},
                        "parent": {"type": "integer"},
                        "symbol": {"type": "string"},
                        "evidence": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["step", "parent", "symbol", "evidence", "note"],
                    "additionalProperties": False,
                },
            },
            "references": {"type": "array", "items": _REF},
            "tests": {"type": "array", "items": _REF},
            "gaps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "explanation", "flow", "references", "tests", "gaps"],
        "additionalProperties": False,
    },
}

ALL_TOOLS = [*TOOLS, SUBMIT_ANSWER]


class ToolError(Exception):
    """A tool failure the model should see and can recover from."""


@dataclass
class ToolOutput:
    label: str
    content: str = ""
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    error: bool = False


def label_for(name: str, args: dict[str, Any]) -> str:
    sym = args.get("symbol") or ""
    path = args.get("path") or ""
    return {
        "repo_overview": "Reading the repository overview",
        "list_dir": f"Listing {path or 'the root directory'}",
        "text_search": f"Searching text for {args.get('pattern', '')!r}",
        "symbol_search": f"Looking up symbols named {args.get('name', '')!r}",
        "semantic_search": f"Searching for code about {args.get('query', '')!r}",
        "get_definition": f"Reading the definition of {sym}",
        "find_references": f"Finding references to {sym}",
        "get_callers": f"Finding callers of {sym}",
        "get_callees": f"Finding what {sym} calls",
        "get_imports": f"Listing imports of {path}",
        "get_importers": f"Finding files that import {path}",
        "find_tests": f"Finding tests for {sym or path}",
        "get_hierarchy": f"Reading the class hierarchy of {sym}",
        "read_code": f"Reading {path} lines {args.get('start_line')}-{args.get('end_line')}",
        "submit_answer": "Writing the answer",
    }.get(name, name)


def _numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{start + i:>5} {line}" for i, line in enumerate(lines))


def _loc(path: str | None, start: int | None, end: int | None = None) -> str:
    if path is None:
        return "external"
    if start is None:
        return path
    return f"{path}:{start}" if end in (None, start) else f"{path}:{start}-{end}"


def _dump(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " ...[result cut off; narrow the query]"
    return text


class Toolbox:
    def __init__(self, session: AsyncSession, repo: Repo, ledger: Ledger):
        self.session = session
        self.repo = repo
        self.ledger = ledger
        self._evidence: list[str] = []

    def _ev(self, path: str | None, start: int | None, end: int | None = None, **kw):
        if path is None:
            return None
        eid = self.ledger.add(path, start or 1, end, **kw)
        if eid is not None and eid not in self._evidence:
            self._evidence.append(eid)
        return eid

    def _code(self, path: str | None, line: int | None) -> str | None:
        if path is None or line is None:
            return None
        lines = self.ledger.lines(path)
        if not lines or not 1 <= line <= len(lines):
            return None
        return lines[line - 1].strip()[:MAX_CODE_CHARS]

    async def run(self, name: str, args: dict[str, Any]) -> ToolOutput:
        out = ToolOutput(label=label_for(name, args))
        self._evidence = []
        handler = getattr(self, f"_t_{name}", None)
        try:
            if handler is None:
                raise ToolError(f"Unknown tool {name!r}.")
            data, out.summary = await handler(**args)
            out.content = _dump(data)
        except (ToolError, SearchError) as exc:
            out.error = True
            out.summary = str(exc)
            out.content = str(exc)
        except TypeError as exc:
            out.error = True
            out.summary = "Bad arguments"
            out.content = f"Bad arguments for {name}: {exc}"
        out.evidence = list(self._evidence)
        return out

    # Symbol and file resolution

    async def _targets(self, symbol: str) -> list[SymbolHit]:
        symbol = (symbol or "").strip()
        if not symbol:
            raise ToolError("Pass a symbol name.")
        m = re.fullmatch(r"(?:id:|#)(\d+)", symbol)
        if m:
            targets = await graph.find_symbols(
                self.session, self.repo.id, symbol_id=int(m.group(1))
            )
        else:
            targets = await graph.find_symbols(self.session, self.repo.id, symbol)
        if targets:
            return targets
        close = await symbol_search(self.session, self.repo.id, symbol, limit=8)
        hint = (
            " Close matches: "
            + ", ".join(f"{h.qualified_name} (id:{h.id})" for h in close)
            if close
            else " Try symbol_search or text_search."
        )
        raise ToolError(f"No symbol named {symbol!r}.{hint}")

    async def _file(self, path: str) -> File:
        file = await graph.file_by_path(self.session, self.repo.id, path)
        if file is None:
            raise ToolError(
                f"No indexed source file at {path!r}. Paths are relative to the repo root."
            )
        return file

    def _target_out(self, t: SymbolHit) -> dict:
        return {
            "id": t.id,
            "symbol": t.qualified_name,
            "kind": t.kind,
            "at": _loc(t.path, t.start_line, t.end_line),
            "evidence_id": self._ev(
                t.path,
                t.start_line,
                t.end_line,
                symbol=t.qualified_name,
                is_test=t.is_test,
            ),
        }

    def _site(self, h: EdgeHit) -> str | None:
        """Evidence for where an edge happens: the line in the source file."""
        return self._ev(
            h.src.path,
            h.line or h.src.start_line,
            None if h.line else h.src.end_line,
            symbol=h.src.qualified_name,
            is_test=h.src.is_test,
        )

    # Tools. Each returns (json-able data, one-line summary for the UI).

    async def _t_repo_overview(self):
        repo = self.repo
        stats = repo.stats or {}
        files = (
            await self.session.execute(
                select(File.path, File.loc, File.is_test).where(File.repo_id == repo.id)
            )
        ).all()
        top: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for path, loc, _ in files:
            head = path.split("/", 1)[0] + ("/" if "/" in path else "")
            top[head][0] += 1
            top[head][1] += loc
        entry_points = [
            p
            for p, _, is_test in files
            if not is_test and ENTRY_POINT_RE.search(p) and p.count("/") <= 3
        ][:15]
        out: dict[str, Any] = {
            "repo": f"{repo.owner}/{repo.name}",
            "sha": repo.sha,
            "languages": stats.get("languages"),
            "source_files": stats.get("source_files"),
            "test_files": stats.get("test_files"),
            "symbols": stats.get("symbols"),
            "top_level": [
                {"path": k, "source_files": v[0], "loc": v[1]}
                for k, v in sorted(top.items(), key=lambda kv: -kv[1][1])[:30]
            ],
            "entry_points": entry_points,
        }
        root = self.ledger.checkout
        manifest = self._manifest(root)
        if manifest:
            out["manifest"] = manifest
        readme = next(
            (
                p.name
                for p in sorted(root.iterdir())
                if p.is_file() and p.name.lower().startswith("readme")
            ),
            None,
        )
        if readme:
            lines = self.ledger.lines(readme) or []
            end = min(len(lines), README_LINES)
            out["readme"] = {
                "path": readme,
                "evidence_id": self._ev(readme, 1, end),
                "text": "\n".join(lines[:end])[:8000],
            }
        return (
            out,
            f"{len(files)} source files, {len(entry_points)} likely entry points",
        )

    def _manifest(self, root) -> dict | None:
        out = {}
        try:
            pkg = json.loads((root / "package.json").read_text("utf-8"))
            out["package.json"] = {
                k: pkg[k]
                for k in (
                    "name",
                    "description",
                    "main",
                    "module",
                    "bin",
                    "exports",
                    "scripts",
                )
                if k in pkg
            }
        except (OSError, ValueError):
            pass
        try:
            py = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
            project = py.get("project", {})
            out["pyproject.toml"] = {
                k: project[k]
                for k in ("name", "description", "scripts")
                if k in project
            }
        except (OSError, ValueError):
            pass
        for section in out.values():
            if len(json.dumps(section.get("scripts", ""))) > 1500:
                section.pop("scripts")
        return out or None

    async def _t_list_dir(self, path: str = ""):
        rel = path.strip().strip("/")
        root = self.ledger.checkout
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_dir():
            raise ToolError(f"No directory at {path!r}.")
        prefix = f"{rel}/" if rel else ""
        indexed = dict(
            (
                await self.session.execute(
                    select(File.path, File.loc).where(
                        File.repo_id == self.repo.id,
                        File.path.startswith(prefix, autoescape=True),
                    )
                )
            ).all()
        )
        dirs, files = [], []
        for p in sorted(target.iterdir(), key=lambda p: p.name):
            if p.name in SKIP_DIRS:
                continue
            child = f"{prefix}{p.name}"
            if p.is_dir():
                n = sum(1 for k in indexed if k.startswith(child + "/"))
                dirs.append({"path": child + "/", "source_files": n})
            elif p.is_file():
                files.append({"path": child, "loc": indexed.get(child)})
        entries = dirs + files
        return {"entries": entries[:200], "truncated": len(entries) > 200}, (
            f"{len(dirs)} directories, {len(files)} files"
        )

    async def _t_text_search(
        self, pattern: str, regex: bool = False, path_glob: str | None = None
    ):
        result = await text_search(
            self.ledger.checkout,
            pattern,
            regex=regex,
            path_glob=path_glob or None,
            limit=50,
        )
        by_file: dict[str, list] = defaultdict(list)
        for m in result.matches:
            by_file[m.path].append(
                {
                    "evidence_id": self._ev(m.path, m.line),
                    "line": m.line,
                    "text": m.text[:MAX_CODE_CHARS],
                }
            )
        return {"files": by_file, "truncated": result.truncated}, (
            f"{len(result.matches)} matches in {len(by_file)} files"
        )

    async def _t_symbol_search(self, name: str, kind: str | None = None):
        hits = await symbol_search(
            self.session, self.repo.id, name, kind=kind or None, limit=20
        )
        out = []
        for h in hits:
            item = self._target_out(h)
            item["signature"] = h.signature
            if h.docstring:
                item["doc"] = h.docstring[:200]
            if h.is_test:
                item["test"] = True
            out.append(item)
        return {"symbols": out}, f"{len(out)} symbols"

    async def _t_semantic_search(self, query: str, k: int = 8):
        k = max(1, min(int(k), 15))
        hits = await semantic_search(self.session, self.repo.id, query, k)
        out = []
        for h in hits:
            lines = h.content.split("\n")
            item = {
                "evidence_id": self._ev(
                    h.path,
                    h.start_line,
                    h.end_line,
                    symbol=h.symbol,
                    is_test=h.is_test,
                    score=h.score,
                ),
                "at": _loc(h.path, h.start_line, h.end_line),
                "symbol": h.symbol,
                "score": h.score,
                "code": _numbered(lines[:MAX_CHUNK_LINES], h.start_line),
            }
            if len(lines) > MAX_CHUNK_LINES:
                item["code"] += "\n  ...[more lines; use read_code]"
            out.append(item)
        return {"chunks": out}, f"{len(out)} code chunks"

    async def _t_get_definition(self, symbol: str):
        targets = await self._targets(symbol)
        defs = await graph.definitions(self.session, self.ledger.checkout, targets)
        out = []
        for d in defs:
            t = d.symbol
            lines = d.source.split("\n")
            item = self._target_out(t)
            item["parent"] = d.parent
            item["signature"] = t.signature
            item["source"] = _numbered(lines[:MAX_DEFINITION_LINES], t.start_line)
            if len(lines) > MAX_DEFINITION_LINES or d.truncated:
                item["source"] += "\n  ...[more lines; use read_code]"
            out.append(item)
        return {"definitions": out}, ", ".join(
            _loc(d.symbol.path, d.symbol.start_line) for d in defs
        )

    async def _t_find_references(self, symbol: str, min_confidence: str = "low"):
        targets = await self._targets(symbol)
        hits, truncated = await graph.references(
            self.session,
            self.repo.id,
            self.ledger.checkout,
            targets,
            min_confidence=min_confidence,
            limit=80,
        )
        by_file: dict[str, list] = defaultdict(list)
        for h in hits:
            by_file[h.src.path or "?"].append(
                {
                    "evidence_id": self._site(h),
                    "line": h.line,
                    "kind": h.kind,
                    "confidence": h.confidence,
                    "in": h.src.qualified_name,
                    "code": h.text[:MAX_CODE_CHARS]
                    if h.text
                    else self._code(h.src.path, h.line),
                }
            )
        return {
            "targets": [self._target_out(t) for t in targets],
            "files": by_file,
            "truncated": truncated,
        }, f"{len(hits)} references in {len(by_file)} files"

    async def _edges(self, fn, symbol: str, min_confidence: str, direction: str):
        targets = await self._targets(symbol)
        hits, truncated = await fn(
            self.session, self.repo.id, targets, min_confidence=min_confidence, limit=60
        )
        out = []
        for h in hits:
            item = {
                "evidence_id": self._site(h),
                "confidence": h.confidence,
                "call": self._code(h.src.path, h.line),
            }
            if direction == "callers":
                item["caller"] = h.src.qualified_name or h.src.name
                item["at"] = _loc(h.src.path, h.line)
                if h.src.is_test:
                    item["test"] = True
            else:
                item["callee"] = h.dst.qualified_name or h.dst.name
                item["callee_id"] = h.dst.symbol_id
                item["defined_at"] = _loc(h.dst.path, h.dst.start_line, h.dst.end_line)
                item["line"] = h.line
            out.append(item)
        return {
            "targets": [self._target_out(t) for t in targets],
            direction: out,
            "truncated": truncated,
        }, f"{len(out)} {direction}"

    async def _t_get_callers(self, symbol: str, min_confidence: str = "low"):
        return await self._edges(graph.callers, symbol, min_confidence, "callers")

    async def _t_get_callees(self, symbol: str, min_confidence: str = "low"):
        return await self._edges(graph.callees, symbol, min_confidence, "callees")

    async def _t_get_imports(self, path: str):
        file = await self._file(path)
        hits, truncated = await graph.imports_of(
            self.session, self.repo.id, file, limit=100
        )
        out = [
            {
                "evidence_id": self._site(h),
                "line": h.line,
                "imports": h.dst.qualified_name or h.dst.name,
                "resolves_to": _loc(h.dst.path, h.dst.start_line),
                "confidence": h.confidence,
            }
            for h in hits
        ]
        return {
            "path": file.path,
            "imports": out,
            "truncated": truncated,
        }, f"{len(out)} imports"

    async def _t_get_importers(self, path: str):
        file = await self._file(path)
        hits, truncated = await graph.importers_of(
            self.session, self.repo.id, file, limit=100
        )
        out = [
            {
                "evidence_id": self._site(h),
                "file": h.src.path,
                "line": h.line,
                "name": h.dst.name,
                "confidence": h.confidence,
            }
            for h in hits
        ]
        files = {h.src.path for h in hits}
        return {"path": file.path, "importers": out, "truncated": truncated}, (
            f"{len(files)} importing files"
        )

    async def _t_find_tests(self, symbol: str | None = None, path: str | None = None):
        targets: list[SymbolHit] = []
        if symbol:
            targets = await self._targets(symbol)
            hits, truncated = await graph.tests_for(
                self.session, self.repo.id, targets, limit=60
            )
        elif path:
            file = await self._file(path)
            hits, truncated = await graph.tests_for(
                self.session, self.repo.id, file=file, limit=60
            )
        else:
            raise ToolError("Pass a symbol or a path.")
        out = []
        for h in hits:
            src = h.src
            if src.symbol_id is not None:
                eid = self._ev(
                    src.path,
                    src.start_line,
                    src.end_line,
                    symbol=src.qualified_name,
                    is_test=True,
                )
                at = _loc(src.path, src.start_line, src.end_line)
            else:
                eid = self._site(h)
                at = _loc(src.path, h.line)
            out.append(
                {
                    "evidence_id": eid,
                    "test": h.label or src.qualified_name or src.name,
                    "at": at,
                    "exercises": h.dst.qualified_name or h.dst.name,
                    "confidence": h.confidence,
                }
            )
        return {
            "targets": [self._target_out(t) for t in targets],
            "tests": out,
            "truncated": truncated,
        }, f"{len(out)} tests"

    async def _t_get_hierarchy(self, symbol: str):
        targets = await self._targets(symbol)
        bases, subclasses, truncated = await graph.hierarchy(
            self.session, self.repo.id, targets, limit=60
        )

        def item(h: EdgeHit, other) -> dict:
            return {
                "evidence_id": self._site(h),
                "class": other.qualified_name or other.name,
                "at": _loc(other.path, other.start_line, other.end_line),
                "relation": h.label or "extends",
                "confidence": h.confidence,
            }

        return {
            "targets": [self._target_out(t) for t in targets],
            "bases": [item(h, h.dst) for h in bases],
            "subclasses": [item(h, h.src) for h in subclasses],
            "truncated": truncated,
        }, f"{len(bases)} bases, {len(subclasses)} subclasses"

    async def _t_read_code(self, path: str, start_line: int, end_line: int):
        path = path.strip().strip("/")
        lines = self.ledger.lines(path)
        if lines is None:
            raise ToolError(f"No file at {path!r}.")
        if not lines:
            return {"path": path, "code": "", "note": "empty file"}, "empty file"
        start = max(1, min(int(start_line), len(lines)))
        end = max(start, min(int(end_line), len(lines)))
        clipped = end - start + 1 > MAX_READ_LINES
        end = min(end, start + MAX_READ_LINES - 1)
        file_id = (
            await self.session.execute(
                select(File.id, File.is_test).where(
                    File.repo_id == self.repo.id, File.path == path
                )
            )
        ).first()
        out = {
            "path": path,
            "evidence_id": self._ev(
                path, start, end, is_test=bool(file_id and file_id[1])
            ),
            "total_lines": len(lines),
            "code": _numbered(lines[start - 1 : end], start),
        }
        if clipped:
            out["note"] = (
                f"Clipped to {MAX_READ_LINES} lines; read the rest separately."
            )
        return out, f"{end - start + 1} lines"
