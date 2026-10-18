"""Resolve per-file facts into imports, calls, references, inherits and tests
edges. Resolution is static and heuristic, so every edge carries a confidence:
high for names bound in the same file or through a resolved import, medium for
a unique match by name across the repo, low for ambiguous matches."""

import json
import logging
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from app.ingest.facts import ImportFact
from app.ingest.parse import ParsedFile, module_name

log = logging.getLogger(__name__)

HIGH, MEDIUM, LOW = "high", "medium", "low"
_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}

MAX_NAME_CANDIDATES = 5
MAX_MEMBER_CANDIDATES = 3
MAX_DEPTH = 10
MAX_STEM_MATCHES = 3

_TYPE_KINDS = {"class", "interface"}
_SELF = {"self", "this", "cls"}
_JS_SRC_EXTS = (".js", ".jsx", ".mjs", ".cjs")
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")
_JS_TS_FOR_JS = (".ts", ".tsx", ".mts", ".cts", ".d.ts")

# Member calls on an unknown receiver with these names are almost always
# builtins (dict.get, list.append, Promise.then), so they are not guessed.
_COMMON_METHODS = {
    "add", "append", "apply", "bind", "call", "catch", "clear", "close", "concat",
    "copy", "count", "decode", "delete", "emit", "encode", "end", "endswith",
    "entries", "every", "exec", "extend", "fill", "filter", "finally", "find",
    "findIndex", "flat", "forEach", "format", "get", "has", "includes", "index",
    "indexOf", "insert", "items", "join", "json", "keys", "lastIndexOf", "log",
    "lower", "map", "match", "next", "on", "once", "pop", "push", "read",
    "reduce", "remove", "replace", "reverse", "search", "send", "set", "setdefault",
    "shift", "slice", "some", "sort", "splice", "split", "startswith", "strip",
    "test", "then", "toLowerCase", "toString", "toUpperCase", "trim", "unshift",
    "update", "upper", "values", "write",
}  # fmt: skip


# Unbound receivers that are language globals, not repo code.
_GLOBALS = {
    "Array", "Buffer", "Date", "Error", "Intl", "JSON", "Map", "Math", "Number",
    "Object", "Promise", "Proxy", "Reflect", "RegExp", "Set", "String", "Symbol",
    "URL", "WeakMap", "console", "document", "globalThis", "navigator", "process",
    "window", "bytes", "dict", "float", "int", "list", "object", "set", "str",
    "tuple", "type",
}  # fmt: skip
_SIDE_DIRS = {"example", "examples", "docs", "demo", "demos", "benchmark", "benchmarks"}


def worst(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


@dataclass(eq=False)
class LinkFile:
    file_id: int
    path: str
    language: str
    is_test: bool
    symbol_ids: list[int | None]  # None when a symbol has no stored row
    parsed: ParsedFile

    @property
    def zone(self) -> str:
        """Name guesses only cross into tests, examples and docs from there."""
        if self.is_test:
            return "test"
        top = self.path.split("/", 1)[0]
        return top if top in _SIDE_DIRS else ""


@dataclass(frozen=True)
class Target:
    file: LinkFile | None = None
    sym: int | None = None  # symbol index in file; None means the module itself
    ext: str | None = None  # dotted name in an external package
    conf: str = HIGH

    def weaken(self, conf: str) -> "Target":
        return replace(self, conf=worst(self.conf, conf))


# Config files: tsconfig paths and workspace package names

_JSONC_RE = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = _JSONC_RE.sub(lambda m: m.group(1) or "", text)
        data = json.loads(_TRAILING_COMMA_RE.sub(r"\1", text))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class _TsConfig:
    dir: str
    base_url: str | None
    paths: list[tuple[str, list[str]]]  # pattern, targets joined to their base


def _ts_options(root: Path, rel: str, depth: int = 0) -> tuple[str | None, list]:
    data = _read_json(root / rel)
    if data is None:
        return None, []
    base_url, paths = None, []
    ext = data.get("extends")
    if isinstance(ext, str) and ext.startswith(".") and depth < 3:
        parent = posixpath.normpath(posixpath.join(posixpath.dirname(rel), ext))
        if not parent.endswith(".json"):
            parent += ".json"
        base_url, paths = _ts_options(root, parent, depth + 1)
    opts = data.get("compilerOptions")
    if not isinstance(opts, dict):
        return base_url, paths
    here = posixpath.dirname(rel)
    if isinstance(opts.get("baseUrl"), str):
        base_url = posixpath.normpath(posixpath.join(here, opts["baseUrl"]))
    if isinstance(opts.get("paths"), dict):
        base = base_url if base_url is not None else here
        paths = [
            (pattern, [posixpath.join(base, t) for t in targets if isinstance(t, str)])
            for pattern, targets in opts["paths"].items()
            if isinstance(targets, list)
        ]
    return base_url, paths


def _load_tsconfigs(root: Path, all_paths: list[str]) -> list[_TsConfig]:
    out = []
    for rel in all_paths:
        name = posixpath.basename(rel)
        if name in ("tsconfig.json", "jsconfig.json") or (
            name.startswith("tsconfig.") and name.endswith(".json")
        ):
            base_url, paths = _ts_options(root, rel)
            if base_url is not None or paths:
                out.append(_TsConfig(posixpath.dirname(rel), base_url, paths))
    # Nearest config first; the plain tsconfig.json before variants.
    out.sort(key=lambda c: -len(c.dir))
    return out


@dataclass
class _Package:
    name: str
    dir: str
    entries: list[str]


def _load_packages(root: Path, all_paths: list[str]) -> list[_Package]:
    out = []
    for rel in all_paths:
        if posixpath.basename(rel) != "package.json":
            continue
        data = _read_json(root / rel)
        if not data or not isinstance(data.get("name"), str):
            continue
        d = posixpath.dirname(rel)
        entries = [posixpath.join(d, "src/index"), posixpath.join(d, "index")]
        for key in ("source", "module", "main", "types"):
            value = data.get(key)
            if isinstance(value, str):
                stem = posixpath.splitext(posixpath.normpath(value))[0]
                for build in ("dist/", "lib/", "build/", "esm/", "cjs/"):
                    if stem.startswith(build):
                        entries.append(posixpath.join(d, "src", stem[len(build) :]))
                entries.append(posixpath.join(d, stem))
        out.append(_Package(data["name"], d, entries))
    out.sort(key=lambda p: -len(p.name))
    return out


def _join(*parts: str) -> str:
    p = posixpath.normpath(posixpath.join(*parts))
    return "" if p == "." else p


_TEST_STEM_RE = re.compile(r"^test_|_test$|^tests?$|\.(test|spec)$")


def _stem(path: str) -> str:
    stem = PurePosixPath(path).name.split(".", 1)[0]
    name = PurePosixPath(path).name
    for suffix in (".test.", ".spec."):
        if suffix in name:
            stem = name.split(suffix, 1)[0]
    return _TEST_STEM_RE.sub("", stem).lower()


def _unique(candidates: list[tuple[LinkFile, int]]) -> list[tuple[LinkFile, int]]:
    """One candidate per qualified name, so overloads count once. The last
    definition wins, as at runtime (the implementation after @overload stubs)."""
    by_key: dict[tuple[str, str], tuple[LinkFile, int]] = {}
    for g, i in candidates:
        by_key[(g.path, g.parsed.symbols[i].qualified_name)] = (g, i)
    return list(by_key.values())


class Resolver:
    def __init__(self, files: list[LinkFile], root: Path, all_paths: list[str]):
        self.files = files
        self.by_path = {f.path: f for f in files}
        self.tsconfigs = _load_tsconfigs(root, all_paths)
        self.packages = _load_packages(root, all_paths)

        self.py_modules: dict[str, LinkFile] = {}
        self.py_suffix: dict[str, list[LinkFile]] = defaultdict(list)
        self.py_prefixes: set[str] = set()
        for f in files:
            if f.language != "python":
                continue
            mod = module_name(f.path, "python")
            current = self.py_modules.get(mod)
            if current is None or current.path.endswith(".pyi"):
                self.py_modules[mod] = f
            parts = mod.split(".")
            for i in range(1, len(parts)):
                self.py_suffix[".".join(parts[i:])].append(f)
                self.py_prefixes.add(".".join(parts[:i]))
            self.py_prefixes.add(mod)

        self.scope: dict[tuple[LinkFile, int], dict[str, int]] = {}
        self.by_name: dict[str, list[tuple[LinkFile, int]]] = defaultdict(list)
        self.bindings: dict[LinkFile, dict[str, tuple[ImportFact, str]]] = {}
        self.stars: dict[LinkFile, list[ImportFact]] = {}
        for f in files:
            symbols = f.parsed.symbols
            for i, s in enumerate(symbols):
                if i == 0 or s.parent is None:
                    continue
                # Last definition wins, as at runtime (overloads, redefinitions).
                self.scope.setdefault((f, s.parent), {})[s.name] = i
                self.by_name[s.name].append((f, i))
            binds: dict[str, tuple[ImportFact, str]] = {}
            stars: list[ImportFact] = []
            for imp in f.parsed.facts.imports:
                if imp.reexport:
                    continue
                for imported, local in imp.names:
                    if local == "*":
                        stars.append(imp)
                    else:
                        binds.setdefault(local, (imp, imported))
            self.bindings[f] = binds
            self.stars[f] = stars

        self._modules: dict[tuple[LinkFile, str], Target | None] = {}
        self._exports: dict[tuple[LinkFile, str], Target | None] = {}
        self._binds: dict[tuple[LinkFile, str], Target | None] = {}
        self._bases: dict[tuple[LinkFile, int], list[Target]] = {}
        self._var_types: dict[tuple[LinkFile, int, str], Target | None] = {}

    # Modules

    def module_target(self, f: LinkFile, source: str) -> Target | None:
        key = (f, source)
        if key not in self._modules:
            if f.language == "python":
                self._modules[key] = self._py_module(f, source)
            else:
                self._modules[key] = self._js_module(f, source)
        return self._modules[key]

    def _py_absolute(self, f: LinkFile, source: str) -> str | None:
        if not source.startswith("."):
            return source
        level = len(source) - len(source.lstrip("."))
        rest = source[level:]
        parts = module_name(f.path, "python").split(".")
        package = parts if PurePosixPath(f.path).stem == "__init__" else parts[:-1]
        if level - 1 > len(package):
            return None
        base = package[: len(package) - (level - 1)]
        if rest:
            base = base + rest.split(".")
        return ".".join(base)

    def _py_lookup(self, name: str) -> Target | None:
        hit = self.py_modules.get(name)
        if hit is not None:
            return Target(file=hit)
        candidates = self.py_suffix.get(name, [])
        if len(candidates) == 1:
            return Target(file=candidates[0], conf=MEDIUM)
        return None

    def _py_is_external(self, source: str, name: str) -> bool:
        if source.startswith("."):
            return False
        return name.split(".", 1)[0] not in self.py_prefixes and not self.py_suffix.get(
            name
        )

    def _py_module(self, f: LinkFile, source: str) -> Target | None:
        name = self._py_absolute(f, source)
        if name is None:
            return None
        hit = self._py_lookup(name)
        if hit is not None:
            return hit
        if self._py_is_external(source, name):
            return Target(ext=name)
        return None

    def _js_file(self, base: str) -> LinkFile | None:
        if base.startswith(".."):
            return None
        if not base:
            return next(
                (
                    self.by_path[c]
                    for c in (f"index{e}" for e in _JS_EXTS)
                    if c in self.by_path
                ),
                None,
            )
        candidates = [base]
        stem, ext = posixpath.splitext(base)
        if ext in _JS_SRC_EXTS:
            candidates += [stem + e for e in _JS_TS_FOR_JS]
        candidates += [base + e for e in _JS_EXTS]
        candidates += [f"{base}/index{e}" for e in _JS_EXTS]
        for c in candidates:
            hit = self.by_path.get(c)
            if hit is not None:
                return hit
        return None

    def _alias_candidates(self, path: str, spec: str) -> list[str]:
        out = []
        for cfg in self.tsconfigs:
            if cfg.dir and not path.startswith(cfg.dir + "/"):
                continue
            for pattern, targets in cfg.paths:
                if "*" in pattern:
                    prefix, _, suffix = pattern.partition("*")
                    if not (
                        spec.startswith(prefix)
                        and spec.endswith(suffix)
                        and len(spec) >= len(prefix) + len(suffix)
                    ):
                        continue
                    star = spec[len(prefix) : len(spec) - len(suffix)]
                    out += [_join(t.replace("*", star)) for t in targets]
                elif pattern == spec:
                    out += [_join(t) for t in targets]
            if cfg.base_url is not None:
                out.append(_join(cfg.base_url, spec))
        return out

    def _js_module(self, f: LinkFile, spec: str) -> Target | None:
        if spec.startswith("."):
            hit = self._js_file(_join(posixpath.dirname(f.path), spec))
            return Target(file=hit) if hit is not None else None
        for base in self._alias_candidates(f.path, spec):
            hit = self._js_file(base)
            if hit is not None:
                return Target(file=hit)
        for pkg in self.packages:
            if spec == pkg.name:
                bases = pkg.entries
            elif spec.startswith(pkg.name + "/"):
                sub = spec[len(pkg.name) + 1 :]
                bases = [_join(pkg.dir, sub), _join(pkg.dir, "src", sub)]
            else:
                continue
            for base in bases:
                hit = self._js_file(base)
                if hit is not None:
                    return Target(file=hit, conf=MEDIUM)
        if spec.startswith("/"):
            return None
        return Target(ext=spec)

    # Names exported by a module

    def export(self, f: LinkFile, name: str, depth: int = 0) -> Target | None:
        key = (f, name)
        if key in self._exports:
            return self._exports[key]
        self._exports[key] = None  # breaks import cycles
        result = self._export(f, name, depth) if depth < MAX_DEPTH else None
        self._exports[key] = result
        return result

    def _export(self, f: LinkFile, name: str, depth: int) -> Target | None:
        facts = f.parsed.facts
        top = self.scope.get((f, 0), {})
        local = facts.exports.get(name)
        if local is None and name == "default":
            local = "exports"  # module.exports = function () {}
        idx = top.get(local or name)
        if idx is not None:
            return Target(file=f, sym=idx)
        if local != "exports" and (local or name) in self.bindings[f]:
            hit = self.bind_target(f, local or name, depth + 1)
            if hit is not None:
                return hit
        for imp in facts.imports:
            star_import = f.language == "python" and ("*", "*") in imp.names
            if not imp.reexport and not star_import:
                continue
            for imported, exported in imp.names:
                if exported not in (name, "*"):
                    continue
                mt = self.module_target(f, imp.source)
                if mt is None or mt.file is None:
                    continue
                if imported == "*" and exported == name:
                    return mt
                hit = self.export(
                    mt.file, name if exported == "*" else imported, depth + 1
                )
                if hit is not None:
                    return hit.weaken(mt.conf)
        if f.language == "python" and PurePosixPath(f.path).stem == "__init__":
            return self._py_lookup(f"{module_name(f.path, 'python')}.{name}")
        return None

    # Names bound in a file

    def bind_target(self, f: LinkFile, local: str, depth: int = 0) -> Target | None:
        key = (f, local)
        if key in self._binds:
            return self._binds[key]
        self._binds[key] = None
        result = self._bind(f, local, depth) if depth < MAX_DEPTH else None
        self._binds[key] = result
        return result

    def _bind(self, f: LinkFile, local: str, depth: int) -> Target | None:
        entry = self.bindings[f].get(local)
        if entry is None:
            for imp in self.stars[f]:
                mt = self.module_target(f, imp.source)
                if mt is not None and mt.file is not None:
                    hit = self.export(mt.file, local, depth + 1)
                    if hit is not None:
                        return hit.weaken(mt.conf)
            return None
        imp, imported = entry
        if f.language == "python" and imported != "*":
            name = self._py_absolute(f, imp.source)
            if name is None:
                return None
            mt = self._py_lookup(name)
            if mt is not None:
                hit = self.export(mt.file, imported, depth + 1)
                if hit is not None:
                    return hit.weaken(mt.conf)
            sub = self._py_lookup(f"{name}.{imported}")
            if sub is not None:
                return sub
            if self._py_is_external(imp.source, name):
                return Target(ext=f"{name}.{imported}")
            return None
        mt = self.module_target(f, imp.source)
        if mt is None:
            return None
        if mt.ext is not None:
            if imported in ("*", "default"):
                return mt
            return Target(ext=f"{mt.ext}.{imported}")
        if imported == "*":
            return mt
        hit = self.export(mt.file, imported, depth + 1)
        return hit.weaken(mt.conf) if hit is not None else None

    # Expressions

    def _kind(self, f: LinkFile, idx: int) -> str:
        return f.parsed.symbols[idx].kind

    def _ancestors(self, f: LinkFile, idx: int) -> list[int]:
        out = []
        cur: int | None = idx
        while cur is not None:
            out.append(cur)
            cur = f.parsed.symbols[cur].parent
        return out

    def enclosing_class(self, f: LinkFile, owner: int) -> int | None:
        for a in self._ancestors(f, owner):
            if self._kind(f, a) == "class":
                return a
        return None

    def name_target(self, f: LinkFile, owner: int, name: str) -> Target | None:
        if "." not in name:
            for a in self._ancestors(f, owner):
                if a != 0 and self._kind(f, a) == "class":
                    continue  # class bodies are not a lexical scope for methods
                idx = self.scope.get((f, a), {}).get(name)
                if idx is not None and self._kind(f, idx) != "method":
                    return Target(file=f, sym=idx)
        if name in self.bindings[f] or ("." not in name and self.stars[f]):
            return self.bind_target(f, name)
        return None

    def var_type(self, f: LinkFile, owner: int, var: str) -> Target | None:
        """The class of a parameter or local from its annotation or `Foo()`."""
        types = f.parsed.facts.var_types
        for a in self._ancestors(f, owner):
            candidates = types.get(a, {}).get(var)
            if not candidates:
                continue
            key = (f, a, var)
            if key not in self._var_types:
                self._var_types[key] = None
                for chain, conf in candidates:
                    hit = next(
                        (
                            t
                            for t in self.resolve(f, a, chain)
                            if t.sym is not None
                            and self._kind(t.file, t.sym) in _TYPE_KINDS
                        ),
                        None,
                    )
                    if hit is not None and hit.conf != LOW:
                        self._var_types[key] = hit.weaken(conf)
                        break
            return self._var_types[key]
        return None

    def bases(self, f: LinkFile, cls: int) -> list[Target]:
        key = (f, cls)
        if key in self._bases:
            return self._bases[key]
        self._bases[key] = []
        owner = f.parsed.symbols[cls].parent or 0
        out = []
        for b in f.parsed.facts.bases:
            if b.symbol == cls:
                out += [
                    t
                    for t in self.resolve(f, owner, b.base)
                    if t.ext is not None
                    or (t.sym is not None and self._kind(t.file, t.sym) in _TYPE_KINDS)
                ]
        self._bases[key] = out
        return out

    def member(self, f: LinkFile, cls: int, name: str, depth: int = 0) -> Target | None:
        idx = self.scope.get((f, cls), {}).get(name)
        if idx is not None:
            return Target(file=f, sym=idx)
        if depth >= MAX_DEPTH:
            return None
        for base in self.bases(f, cls):
            if base.file is None or base.sym is None:
                continue
            hit = self.member(base.file, base.sym, name, depth + 1)
            if hit is not None:
                return hit.weaken(base.conf)
        return None

    def descend(self, t: Target, name: str) -> Target | None:
        if t.ext is not None:
            return Target(ext=f"{t.ext}.{name}")
        if t.file is None:
            return None
        if t.sym is None:
            hit = self.export(t.file, name)
        elif self._kind(t.file, t.sym) in _TYPE_KINDS:
            hit = self.member(t.file, t.sym, name)
        else:
            hit = None
        return hit.weaken(t.conf) if hit is not None else None

    def _reachable(self, f: LinkFile, g: LinkFile) -> bool:
        return g is f or not g.zone or g.zone == f.zone

    def global_name(self, f: LinkFile, name: str) -> list[Target]:
        candidates = [
            (g, i) for g, i in self.by_name.get(name, []) if self._reachable(f, g)
        ]
        candidates = [
            (g, i)
            for g, i in candidates
            if self._kind(g, i) == "class"
            or (self._kind(g, i) == "function" and g.parsed.symbols[i].parent == 0)
        ]
        candidates = _unique(candidates)
        if not candidates or len(candidates) > MAX_NAME_CANDIDATES:
            return []
        conf = MEDIUM if len(candidates) == 1 else LOW
        return [Target(file=g, sym=i, conf=conf) for g, i in candidates]

    def global_member(self, f: LinkFile, name: str) -> list[Target]:
        if name in _COMMON_METHODS or (name.startswith("__") and name.endswith("__")):
            return []
        candidates = [
            (g, i)
            for g, i in self.by_name.get(name, [])
            if self._kind(g, i) in ("method", "function") and self._reachable(f, g)
        ]
        candidates = _unique(candidates)
        same_file = [(g, i) for g, i in candidates if g is f]
        if len(same_file) == 1:
            return [Target(file=f, sym=same_file[0][1], conf=MEDIUM)]
        if not candidates or len(candidates) > MAX_MEMBER_CANDIDATES:
            return []
        return [Target(file=g, sym=i, conf=LOW) for g, i in candidates]

    def resolve(self, f: LinkFile, owner: int, chain: str) -> list[Target]:
        parts = chain.split(".")
        head = parts[0]
        if head in _SELF:
            cls = self.enclosing_class(f, owner)
            if len(parts) == 2 and cls is not None:
                hit = self.member(f, cls, parts[1])
                if hit is not None:
                    return [hit]
            return self.global_member(f, parts[-1]) if len(parts) > 1 else []
        if head == "super":
            cls = self.enclosing_class(f, owner)
            if cls is None:
                return []
            if len(parts) > 1:
                name = parts[1]
            else:
                name = "__init__" if f.language == "python" else "constructor"
            for base in self.bases(f, cls):
                if base.file is not None and base.sym is not None:
                    hit = self.member(base.file, base.sym, name)
                    if hit is not None:
                        return [hit.weaken(base.conf)]
                    if len(parts) == 1:
                        return [base]
            return []
        if head == "?":
            return self.global_member(f, parts[-1]) if len(parts) > 1 else []

        target = self.var_type(f, owner, head) if len(parts) > 1 else None
        k = 1 if target is not None else len(parts)
        while target is None and k > 0:
            target = self.name_target(f, owner, ".".join(parts[:k]))
            if target is not None:
                break
            k -= 1
        if target is None:
            if len(parts) == 1:
                return self.global_name(f, head)
            if head in _GLOBALS:
                return []
            return self.global_member(f, parts[-1])
        for p in parts[k:]:
            nxt = self.descend(target, p)
            if nxt is None:
                return self.global_member(f, parts[-1]) if target.ext is None else []
            target = nxt
        return [target]


class _Edges:
    def __init__(self, repo_id: int) -> None:
        self.repo_id = repo_id
        self.rows: list[dict[str, Any]] = []
        self.seen: set[tuple] = set()

    def add(
        self,
        kind: str,
        src: LinkFile,
        src_symbol: int | None,
        target: Target | None,
        dst_name: str,
        line: int | None,
        conf: str,
        src_name: str | None = None,
    ) -> None:
        dst_file = target.file if target is not None else None
        dst_symbol_id = None
        if dst_file is not None and target.sym is not None:
            dst_symbol_id = dst_file.symbol_ids[target.sym]
            if dst_symbol_id is None:
                return
        src_symbol_id = None
        while src_symbol is not None:
            src_symbol_id = src.symbol_ids[src_symbol]
            if src_symbol_id is not None:
                break
            src_symbol = src.parsed.symbols[src_symbol].parent
        if dst_symbol_id is not None and dst_symbol_id == src_symbol_id:
            return
        key = (
            kind,
            src.file_id,
            src_symbol_id,
            src_name,
            dst_file.file_id if dst_file else None,
            dst_symbol_id,
            None if dst_file else dst_name,
        )
        if key in self.seen:
            return
        self.seen.add(key)
        self.rows.append(
            {
                "repo_id": self.repo_id,
                "kind": kind,
                "src_file_id": src.file_id,
                "src_symbol_id": src_symbol_id,
                "src_name": src_name,
                "dst_file_id": dst_file.file_id if dst_file else None,
                "dst_symbol_id": dst_symbol_id,
                "dst_name": dst_name[:500],
                "line": line,
                "confidence": conf,
            }
        )


def _module_call_target(r: Resolver, t: Target) -> Target | None:
    """Calling a whole module (CommonJS `const m = require('./m'); m()`)."""
    if t.file is None or t.sym is not None:
        return t
    hit = r.export(t.file, "default")
    return hit.weaken(t.conf) if hit is not None else None


def build_edges(
    repo_id: int, files: list[LinkFile], root: Path, all_paths: list[str]
) -> list[dict[str, Any]]:
    r = Resolver(files, root, all_paths)
    edges = _Edges(repo_id)
    # (src file, src symbol, test title, test line, target, line, conf)
    test_hits: list[tuple] = []

    for f in files:
        facts = f.parsed.facts
        for imp in facts.imports:
            mt = r.module_target(f, imp.source)
            if mt is None:
                edges.add("imports", f, imp.symbol, None, imp.source, imp.line, LOW)
                continue
            if mt.ext is not None:
                edges.add("imports", f, imp.symbol, None, imp.source, imp.line, HIGH)
                continue
            edges.add("imports", f, imp.symbol, mt, imp.source, imp.line, mt.conf)
            if imp.reexport:
                continue
            for imported, local in imp.names:
                if local == "*" or imported == "*":
                    continue
                t = r.bind_target(f, local)
                if t is not None and t.file is not None and t.sym is not None:
                    edges.add(
                        "imports", f, imp.symbol, t, f"{imp.source}:{imported}",
                        imp.line, t.conf,
                    )  # fmt: skip

        for b in facts.bases:
            kind_note = "implements" if b.implements else None
            owner = f.parsed.symbols[b.symbol].parent or 0
            for t in r.resolve(f, owner, b.base):
                if t.ext is not None:
                    edges.add(
                        "inherits", f, b.symbol, None, t.ext, b.line, t.conf, kind_note
                    )
                elif t.sym is not None:
                    edges.add(
                        "inherits", f, b.symbol, t, b.base, b.line, t.conf, kind_note
                    )

        for c in facts.calls:
            for t in r.resolve(f, c.symbol, c.callee):
                if t.ext is not None:
                    edges.add("calls", f, c.symbol, None, t.ext, c.line, t.conf)
                    continue
                t = _module_call_target(r, t)
                if t is None:
                    continue
                edges.add("calls", f, c.symbol, t, c.callee, c.line, t.conf)
                if f.is_test and t.conf != LOW:
                    test_hits.append(
                        (f, c.symbol, c.test, c.test_line or c.line, t, c.callee)
                    )

        for ref in facts.refs:
            t = r.name_target(f, ref.symbol, ref.name)
            if t is None or t.file is None:
                continue
            if (
                t.sym is not None
                and t.sym in r._ancestors(f, ref.symbol)
                and t.file is f
            ):
                continue
            edges.add("references", f, ref.symbol, t, ref.name, ref.line, t.conf)
            if f.is_test and t.sym is not None and t.conf != LOW:
                test_hits.append(
                    (f, ref.symbol, ref.test, ref.test_line or ref.line, t, ref.name)
                )

    for f, sym, title, line, t, name in test_hits:
        if t.file is None or t.file.is_test:
            continue
        edges.add("tests", f, sym, t, name, line, t.conf, title)

    # File-level tests by name: tests/test_auth.py, auth.test.ts -> auth.py, auth.ts
    by_stem: dict[str, list[LinkFile]] = defaultdict(list)
    for f in files:
        if not f.is_test:
            by_stem[_stem(f.path)].append(f)
    for f in files:
        if not f.is_test:
            continue
        stem = _stem(f.path)
        matches = by_stem.get(stem, []) if stem else []
        if 0 < len(matches) <= MAX_STEM_MATCHES:
            conf = MEDIUM if len(matches) == 1 else LOW
            for m in matches:
                edges.add(
                    "tests", f, 0, Target(file=m), m.path, None, conf, "file name"
                )
    return edges.rows
