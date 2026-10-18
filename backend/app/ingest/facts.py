"""Raw code-intelligence facts from one parsed file: imports, call sites,
base classes and name references. Resolution into edges happens later in
resolve.py, once every file of the repo is known."""

import re
import threading
from dataclasses import dataclass, field

from tree_sitter import Node, Query, QueryCursor
from tree_sitter_language_pack import get_language

from app.ingest.walk import is_test_path

MAX_CHAIN = 200
MAX_TEST_TITLE = 200

_local = threading.local()


@dataclass
class ImportFact:
    symbol: int  # index of the enclosing symbol
    line: int
    source: str  # Python module (".a.b", "x.y") or JS specifier ("./x", "react")
    # (imported, local). imported "*" binds the whole module to local, or with
    # local "*" is a star import. JS default imports use imported "default".
    names: list[tuple[str, str]] = field(default_factory=list)
    reexport: bool = False  # JS `export ... from`; names are (imported, exported)


@dataclass
class CallFact:
    symbol: int
    line: int
    callee: str  # dotted chain: foo, self.save, models.User, ?.json, super
    kind: str = "call"  # call, new or jsx
    test: str | None = None  # title of the enclosing it()/test() in JS tests
    test_line: int | None = None


@dataclass
class RefFact:
    symbol: int
    line: int
    name: str
    test: str | None = None
    test_line: int | None = None


@dataclass
class BaseFact:
    symbol: int  # the class or interface
    line: int
    base: str
    implements: bool = False


@dataclass
class Facts:
    imports: list[ImportFact] = field(default_factory=list)
    calls: list[CallFact] = field(default_factory=list)
    refs: list[RefFact] = field(default_factory=list)
    bases: list[BaseFact] = field(default_factory=list)
    exports: dict[str, str] = field(default_factory=dict)  # exported -> local name
    # symbol -> variable -> candidate type chains with a confidence, from
    # annotations (high) and `x = Foo()` / `new Foo()` assignments (medium)
    var_types: dict[int, dict[str, list[tuple[str, str]]]] = field(default_factory=dict)


_PY_QUERY = """
(import_statement) @import
(import_from_statement) @import
(call) @call
(class_definition) @class
(typed_parameter) @param
(typed_default_parameter) @param
(assignment) @pyassign
(identifier) @ident
"""

_JS_QUERY = """
(import_statement) @import
(export_statement) @export
(call_expression) @call
(new_expression) @new
(class_heritage) @heritage
(assignment_expression) @assign
(variable_declarator) @vardecl
(identifier) @ident
"""

_TS_QUERY = """
(extends_type_clause) @heritage
(required_parameter) @param
(optional_parameter) @param
(type_identifier) @ident
"""

_JSX_QUERY = """
(jsx_opening_element) @jsx
(jsx_self_closing_element) @jsx
"""

_QUERIES = {
    "python": _PY_QUERY,
    "javascript": _JS_QUERY + _JSX_QUERY,
    "typescript": _JS_QUERY + _TS_QUERY,
    "tsx": _JS_QUERY + _TS_QUERY + _JSX_QUERY,
}

_TEST_CALLS = {"it", "test", "specify"}

_TYPE_NAME_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
# Wrappers and builtins skipped when reading a type annotation.
_TYPE_SKIP = {
    "Any", "Array", "Awaitable", "Callable", "Dict", "Iterable", "Iterator",
    "List", "Mapping", "None", "Optional", "Partial", "Promise", "Readonly",
    "Record", "Sequence", "Set", "T", "Tuple", "Type", "Union", "any", "bool",
    "boolean", "bytes", "dict", "float", "int", "list", "null", "number",
    "object", "set", "str", "string", "t", "tuple", "type", "typing",
    "undefined", "unknown", "void",
}  # fmt: skip


def _query(language: str) -> Query:
    cache = getattr(_local, "queries", None)
    if cache is None:
        cache = _local.queries = {}
    if language not in cache:
        cache[language] = Query(get_language(language), _QUERIES[language])
    return cache[language]


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point.row + 1


def _is_field(node: Node, field_name: str) -> bool:
    parent = node.parent
    if parent is None:
        return False
    child = parent.child_by_field_name(field_name)
    return child is not None and child.id == node.id


class _Extractor:
    def __init__(self, path: str, language: str, symbols: list) -> None:
        self.language = language
        self.is_test = is_test_path(path)
        self.facts = Facts()
        self.spans = {s.span: i for i, s in enumerate(symbols) if i > 0}
        self.names = {s.name for s in symbols[1:] if s.kind != "method"}

    def owner(self, node: Node) -> int:
        n: Node | None = node
        while n is not None:
            idx = self.spans.get((n.start_byte, n.end_byte))
            if idx is not None:
                return idx
            n = n.parent
        return 0

    def js_test(self, node: Node) -> tuple[str | None, int | None]:
        if not self.is_test:
            return None, None
        n = node.parent
        while n is not None:
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                head = fn
                while head is not None and head.type == "member_expression":
                    head = head.child_by_field_name("object")
                if head is not None and _text(head) in _TEST_CALLS:
                    args = n.child_by_field_name("arguments")
                    first = (
                        args.named_children[0] if args and args.named_children else None
                    )
                    if first is not None and first.type in (
                        "string",
                        "template_string",
                    ):
                        title = _text(first)[1:-1].strip()[:MAX_TEST_TITLE]
                        return title or None, _line(n)
            n = n.parent
        return None, None

    def add_call(self, node: Node, callee: str, kind: str = "call") -> None:
        if not callee or callee == "?" or len(callee) > MAX_CHAIN:
            return
        test, test_line = (None, None)
        if self.language != "python":
            test, test_line = self.js_test(node)
        self.facts.calls.append(
            CallFact(self.owner(node), _line(node), callee, kind, test, test_line)
        )

    def add_type(self, node: Node, var: str, chain: str, conf: str) -> None:
        if not var or not chain or chain == "?":
            return
        by_var = self.facts.var_types.setdefault(self.owner(node), {})
        by_var.setdefault(var, []).append((chain, conf))

    def add_annotation(self, node: Node, var: str, annotation: Node | None) -> None:
        for chain in _TYPE_NAME_RE.findall(_text(annotation)):
            if chain.rsplit(".", 1)[-1] not in _TYPE_SKIP:
                self.add_type(node, var, chain, "high")

    def add_ref(self, node: Node, name: str) -> None:
        test, test_line = (None, None)
        if self.language != "python":
            test, test_line = self.js_test(node)
        self.facts.refs.append(
            RefFact(self.owner(node), _line(node), name, test, test_line)
        )


# Python


def _py_chain(node: Node | None) -> str:
    if node is None:
        return "?"
    t = node.type
    if t == "identifier":
        return _text(node)
    if t == "attribute":
        obj = _py_chain(node.child_by_field_name("object"))
        return f"{obj}.{_text(node.child_by_field_name('attribute'))}"
    if t == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "identifier" and _text(fn) == "super":
            return "super"
    if t == "subscript":
        return _py_chain(node.child_by_field_name("value"))
    return "?"


def _py_import(x: _Extractor, node: Node) -> None:
    owner = x.owner(node)
    line = _line(node)
    if node.type == "import_statement":
        for name in node.children_by_field_name("name"):
            if name.type == "aliased_import":
                module = _text(name.child_by_field_name("name"))
                local = _text(name.child_by_field_name("alias"))
            else:
                module = local = _text(name)
            x.facts.imports.append(ImportFact(owner, line, module, [("*", local)]))
        return
    module = "".join(_text(node.child_by_field_name("module_name")).split())
    names = []
    for name in node.children_by_field_name("name"):
        if name.type == "aliased_import":
            names.append(
                (
                    _text(name.child_by_field_name("name")),
                    _text(name.child_by_field_name("alias")),
                )
            )
        else:
            names.append((_text(name), _text(name)))
    if any(c.type == "wildcard_import" for c in node.children):
        names.append(("*", "*"))
    x.facts.imports.append(ImportFact(owner, line, module, names))


_PY_SKIP_PARENTS = {
    "dotted_name",
    "aliased_import",
    "parameters",
    "typed_parameter",
    "lambda_parameters",
    "global_statement",
    "nonlocal_statement",
}


def _py_ref(x: _Extractor, node: Node) -> None:
    parent = node.parent
    if parent is None or parent.type in _PY_SKIP_PARENTS:
        return
    if parent.type == "attribute" and _is_field(node, "attribute"):
        return
    if parent.type == "call" and _is_field(node, "function"):
        return
    if _is_field(node, "name") or _is_field(node, "alias"):
        return
    x.add_ref(node, _text(node))


def _extract_python(x: _Extractor, captures: dict[str, list[Node]]) -> None:
    for node in captures.get("import", []):
        _py_import(x, node)
    for node in captures.get("call", []):
        callee = _py_chain(node.child_by_field_name("function"))
        if callee != "super":
            x.add_call(node, callee)
    for node in captures.get("param", []):
        name = node.child_by_field_name("name")
        if name is None:
            name = next(
                (c for c in node.named_children if c.type == "identifier"), None
            )
        x.add_annotation(node, _text(name), node.child_by_field_name("type"))
    for node in captures.get("pyassign", []):
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            continue
        annotation = node.child_by_field_name("type")
        right = node.child_by_field_name("right")
        if annotation is not None:
            x.add_annotation(node, _text(left), annotation)
        elif right is not None and right.type == "call":
            chain = _py_chain(right.child_by_field_name("function"))
            if chain.rsplit(".", 1)[-1][:1].isupper():
                x.add_type(node, _text(left), chain, "medium")
    for node in captures.get("class", []):
        supers = node.child_by_field_name("superclasses")
        if supers is None:
            continue
        owner = x.owner(node)
        for arg in supers.named_children:
            if arg.type in ("identifier", "attribute", "subscript"):
                base = _py_chain(arg)
                if base != "?":
                    x.facts.bases.append(BaseFact(owner, _line(arg), base))
    x.names.update(
        local for imp in x.facts.imports for _, local in imp.names if local != "*"
    )
    for node in captures.get("ident", []):
        if _text(node) in x.names:
            _py_ref(x, node)


# JavaScript / TypeScript

_JS_NAME_TYPES = {
    "identifier",
    "property_identifier",
    "type_identifier",
    "shorthand_property_identifier",
    "private_property_identifier",
}


def _js_chain(node: Node | None) -> str:
    if node is None:
        return "?"
    t = node.type
    if t in _JS_NAME_TYPES:
        return _text(node)
    if t in ("this", "super"):
        return t
    if t == "member_expression":
        obj = _js_chain(node.child_by_field_name("object"))
        return f"{obj}.{_text(node.child_by_field_name('property'))}"
    if t == "nested_type_identifier":
        return "".join(_text(node).split())
    if t in ("generic_type", "parenthesized_expression", "non_null_expression"):
        return _js_chain(node.named_children[0]) if node.named_children else "?"
    return "?"


def _js_string(node: Node | None) -> str | None:
    if node is None or node.type != "string":
        return None
    return "".join(_text(c) for c in node.named_children if c.type == "string_fragment")


def _js_specifiers(node: Node) -> list[tuple[str, str]]:
    out = []
    for spec in node.named_children:
        if spec.type in ("import_specifier", "export_specifier"):
            name = _text(spec.child_by_field_name("name"))
            alias = spec.child_by_field_name("alias")
            out.append((name, _text(alias) if alias is not None else name))
    return out


def _js_import(x: _Extractor, node: Node) -> None:
    source = _js_string(node.child_by_field_name("source"))
    if source is None:
        return
    names = []
    for clause in node.named_children:
        if clause.type != "import_clause":
            continue
        for c in clause.named_children:
            if c.type == "identifier":
                names.append(("default", _text(c)))
            elif c.type == "namespace_import":
                ident = next(
                    (n for n in c.named_children if n.type == "identifier"), None
                )
                if ident is not None:
                    names.append(("*", _text(ident)))
            elif c.type == "named_imports":
                names.extend(_js_specifiers(c))
    x.facts.imports.append(ImportFact(x.owner(node), _line(node), source, names))


def _js_export(x: _Extractor, node: Node) -> None:
    source = _js_string(node.child_by_field_name("source"))
    clause = next((c for c in node.named_children if c.type == "export_clause"), None)
    if source is not None:
        names = _js_specifiers(clause) if clause is not None else []
        ns = next(
            (c for c in node.named_children if c.type == "namespace_export"), None
        )
        if ns is not None:
            names.append(
                ("*", _text(ns.named_children[-1]) if ns.named_children else "*")
            )
        elif clause is None:
            names.append(("*", "*"))
        x.facts.imports.append(
            ImportFact(x.owner(node), _line(node), source, names, reexport=True)
        )
        return
    if clause is not None:
        for name, alias in _js_specifiers(clause):
            x.facts.exports[alias] = name
    if any(c.type == "default" for c in node.children):
        decl = node.child_by_field_name("declaration") or node.child_by_field_name(
            "value"
        )
        if decl is None:
            return
        name = decl if decl.type == "identifier" else decl.child_by_field_name("name")
        if name is not None:
            x.facts.exports["default"] = _text(name)


def _js_require(x: _Extractor, node: Node, args: Node | None) -> None:
    first = args.named_children[0] if args and args.named_children else None
    source = _js_string(first)
    if source is None:
        return
    names: list[tuple[str, str]] = []
    target = node.parent
    if target is not None and target.type == "assignment_expression":
        left = "".join(_text(target.child_by_field_name("left")).split())
        if left in ("module.exports", "exports"):
            # module.exports = require('./lib/x') re-exports everything
            x.facts.imports.append(
                ImportFact(x.owner(node), _line(node), source, [("*", "*")], True)
            )
            return
    member = None
    if target is not None and target.type == "member_expression":
        member = _text(target.child_by_field_name("property"))
        target = target.parent
    if target is not None and target.type == "variable_declarator":
        bound = target.child_by_field_name("name")
        if bound is not None and bound.type == "identifier":
            names.append((member or "*", _text(bound)))
        elif bound is not None and bound.type == "object_pattern" and member is None:
            for p in bound.named_children:
                if p.type == "shorthand_property_identifier_pattern":
                    names.append((_text(p), _text(p)))
                elif p.type == "pair_pattern":
                    key = p.child_by_field_name("key")
                    value = p.child_by_field_name("value")
                    if value is not None and value.type == "identifier":
                        names.append((_text(key), _text(value)))
    x.facts.imports.append(ImportFact(x.owner(node), _line(node), source, names))


def _js_call(x: _Extractor, node: Node) -> None:
    fn = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")
    if fn is not None and fn.type == "import":
        source = _js_string(
            args.named_children[0] if args and args.named_children else None
        )
        if source is not None:
            x.facts.imports.append(ImportFact(x.owner(node), _line(node), source))
        return
    if fn is not None and fn.type == "identifier" and _text(fn) == "require":
        _js_require(x, node, args)
        return
    x.add_call(node, _js_chain(fn))


def _js_assign(x: _Extractor, node: Node) -> None:
    """CommonJS exports: module.exports = foo, exports.bar = baz, = {a, b}."""
    left = "".join(_text(node.child_by_field_name("left")).split())
    right = node.child_by_field_name("right")
    if right is None:
        return
    if left in ("module.exports", "exports"):
        if right.type == "identifier":
            x.facts.exports["default"] = _text(right)
        elif right.type == "object":
            for p in right.named_children:
                if p.type == "shorthand_property_identifier":
                    x.facts.exports[_text(p)] = _text(p)
                elif p.type == "pair":
                    value = p.child_by_field_name("value")
                    if value is not None and value.type == "identifier":
                        key = _text(p.child_by_field_name("key")).strip("'\"")
                        x.facts.exports[key] = _text(value)
    elif (
        left.startswith(("module.exports.", "exports.")) and right.type == "identifier"
    ):
        x.facts.exports[left.rsplit(".", 1)[1]] = _text(right)


def _js_heritage(x: _Extractor, node: Node) -> None:
    owner = x.owner(node.parent) if node.parent is not None else 0
    if owner == 0:
        return

    def add(n: Node, implements: bool) -> None:
        base = _js_chain(n)
        if base != "?":
            x.facts.bases.append(BaseFact(owner, _line(n), base, implements))

    for c in node.named_children:
        if c.type == "extends_clause":
            for v in c.children_by_field_name("value"):
                add(v, False)
        elif c.type == "implements_clause":
            for v in c.named_children:
                add(v, True)
        elif node.type == "extends_type_clause":
            add(c, False)
        elif c.type not in ("comment", "type_arguments"):
            add(c, False)  # plain JS: class_heritage holds the expression


_JS_SKIP_PARENTS = {
    "import_specifier",
    "import_clause",
    "namespace_import",
    "export_specifier",
    "formal_parameters",
    "required_parameter",
    "optional_parameter",
}


def _js_ref(x: _Extractor, node: Node) -> None:
    parent = node.parent
    if parent is None or parent.type in _JS_SKIP_PARENTS:
        return
    if parent.type == "call_expression" and _is_field(node, "function"):
        return
    if parent.type == "new_expression" and _is_field(node, "constructor"):
        return
    if parent.type.startswith("jsx_"):
        return
    if _is_field(node, "name") or _is_field(node, "alias"):
        return
    if parent.type in ("extends_clause", "implements_clause", "class_heritage"):
        return
    x.add_ref(node, _text(node))


def _extract_js(x: _Extractor, captures: dict[str, list[Node]]) -> None:
    for node in captures.get("import", []):
        _js_import(x, node)
    for node in captures.get("export", []):
        _js_export(x, node)
    for node in captures.get("assign", []):
        _js_assign(x, node)
    for node in captures.get("call", []):
        _js_call(x, node)
    for node in captures.get("new", []):
        x.add_call(node, _js_chain(node.child_by_field_name("constructor")), "new")
    for node in captures.get("jsx", []):
        name = node.child_by_field_name("name")
        if name is None or name.type not in ("identifier", "member_expression"):
            continue
        chain = _js_chain(name)
        if chain[:1].isupper() or "." in chain:
            x.add_call(node, chain, "jsx")
    for node in captures.get("heritage", []):
        _js_heritage(x, node)
    for node in captures.get("param", []):
        pattern = node.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "identifier":
            x.add_annotation(node, _text(pattern), node.child_by_field_name("type"))
    for node in captures.get("vardecl", []):
        name = node.child_by_field_name("name")
        if name is None or name.type != "identifier":
            continue
        annotation = node.child_by_field_name("type")
        value = node.child_by_field_name("value")
        if annotation is not None:
            x.add_annotation(node, _text(name), annotation)
        elif value is not None and value.type == "new_expression":
            chain = _js_chain(value.child_by_field_name("constructor"))
            x.add_type(node, _text(name), chain, "medium")
    x.names.update(
        local for imp in x.facts.imports for _, local in imp.names if local != "*"
    )
    for node in captures.get("ident", []):
        if _text(node) in x.names:
            _js_ref(x, node)


def _dedupe(facts: Facts) -> None:
    for items in (facts.imports, facts.calls, facts.refs, facts.bases):
        items.sort(key=lambda f: f.line)
    seen: set = set()
    calls = []
    for c in facts.calls:
        key = (c.symbol, c.callee, c.kind, c.test)
        if key not in seen:
            seen.add(key)
            calls.append(c)
    facts.calls = calls
    seen = set()
    refs = []
    for r in facts.refs:
        key = (r.symbol, r.name, r.test)
        if key not in seen:
            seen.add(key)
            refs.append(r)
    facts.refs = refs


def extract_facts(path: str, language: str, root: Node, symbols: list) -> Facts:
    x = _Extractor(path, language, symbols)
    captures = QueryCursor(_query(language)).captures(root)
    if language == "python":
        _extract_python(x, captures)
    else:
        _extract_js(x, captures)
    _dedupe(x.facts)
    return x.facts
