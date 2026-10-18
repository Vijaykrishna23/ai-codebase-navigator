import inspect
import re
import threading
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from tree_sitter import Node, Parser
from tree_sitter_language_pack import get_parser

from app.ingest.facts import Facts, extract_facts

MAX_DOCSTRING = 2000
MAX_SIGNATURE = 300

_local = threading.local()
_WS_RE = re.compile(r"\s+")


@dataclass
class ParsedSymbol:
    kind: str
    name: str
    qualified_name: str
    parent: int | None
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    span: tuple[int, int] = (0, 0)  # byte range of the defining node


@dataclass
class ParsedFile:
    symbols: list[ParsedSymbol] = field(default_factory=list)
    has_error: bool = False
    facts: Facts = field(default_factory=Facts)


def _parser(language: str) -> Parser:
    cache = getattr(_local, "parsers", None)
    if cache is None:
        cache = _local.parsers = {}
    if language not in cache:
        cache[language] = get_parser(language)
    return cache[language]


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _between(src: bytes, start: int, end: int) -> str:
    sig = src[start:end].decode("utf-8", errors="replace")
    sig = _WS_RE.sub(" ", sig).strip().rstrip("{:").rstrip()
    return sig[:MAX_SIGNATURE]


def _clip(doc: str) -> str | None:
    doc = doc.strip()
    return doc[:MAX_DOCSTRING] if doc else None


def module_name(path: str, language: str) -> str:
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if language == "python":
        if parts and parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        if parts[-1] == "__init__" and len(parts) > 1:
            parts = parts[:-1]
        return ".".join(parts)
    if parts[-1] == "index" and len(parts) > 1:
        parts = parts[:-1]
    return "/".join(parts)


class _Builder:
    def __init__(self, module: str, sep: str, total_lines: int, module_doc: str | None):
        self.sep = sep
        self.chains: list[list[str]] = [[]]
        self.symbols = [
            ParsedSymbol(
                kind="module",
                name=module.rsplit("/", 1)[-1].rsplit(".", 1)[-1],
                qualified_name=module,
                parent=None,
                start_line=1,
                end_line=max(total_lines, 1),
                docstring=module_doc,
            )
        ]
        self.module = module

    def add(
        self,
        kind: str,
        name: str,
        parent: int,
        node: Node,
        signature: str | None,
        docstring: str | None,
        chain_name: str | None = None,
    ) -> int:
        chain = [*self.chains[parent], chain_name or name]
        self.chains.append(chain)
        self.symbols.append(
            ParsedSymbol(
                kind=kind,
                name=name,
                qualified_name=f"{self.module}{self.sep}{'.'.join(chain)}",
                parent=parent,
                start_line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature=signature,
                docstring=docstring,
                span=(node.start_byte, node.end_byte),
            )
        )
        return len(self.symbols) - 1


# Python


def _py_docstring(block: Node | None) -> str | None:
    if block is None or not block.named_children:
        return None
    string = block.named_children[0]
    if string.type == "expression_statement" and string.named_children:
        string = string.named_children[0]
    if string.type != "string":
        return None
    content = "".join(
        _text(c) for c in string.named_children if c.type == "string_content"
    )
    return _clip(inspect.cleandoc(content))


def _py_visit(b: _Builder, src: bytes, node: Node, parent: int, scope: str) -> None:
    for child in node.named_children:
        target = child
        if child.type == "decorated_definition":
            target = child.child_by_field_name("definition") or child
        if target.type in ("function_definition", "class_definition"):
            name = _text(target.child_by_field_name("name"))
            body = target.child_by_field_name("body")
            if target.type == "class_definition":
                kind = "class"
            else:
                kind = "method" if scope == "class" else "function"
            sig_end = body.start_byte if body else target.end_byte
            idx = b.add(
                kind,
                name,
                parent,
                child,
                _between(src, target.start_byte, sig_end),
                _py_docstring(body),
            )
            if body is not None:
                _py_visit(b, src, body, idx, "class" if kind == "class" else "function")
        elif child.type == "block" or child.type.endswith(("_statement", "_clause")):
            # Conditional and try/except definitions still count.
            _py_visit(b, src, child, parent, scope)


def _parse_python(b_args: dict, src: bytes, root: Node) -> _Builder:
    b = _Builder(sep=".", module_doc=_py_docstring(root), **b_args)
    _py_visit(b, src, root, 0, "module")
    return b


# JavaScript / TypeScript

_JS_FUNCTION_VALUES = {
    "arrow_function",
    "function_expression",
    "function",
    "generator_function",
}
_JS_CLASS_DECLS = {"class_declaration", "abstract_class_declaration", "class"}
_TS_TYPE_DECLS = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_JS_WRAPPERS = {"export_statement", "lexical_declaration", "variable_declaration"}
_JS_VALUE_WRAPPERS = {
    "as_expression",
    "satisfies_expression",
    "parenthesized_expression",
    "non_null_expression",
}
_JS_FUNCTION_WRAPPERS = {
    "memo",
    "forwardRef",
    "React.memo",
    "React.forwardRef",
    "observer",
    "useCallback",
}


def _js_unwrap(value: Node | None) -> Node | None:
    """`(() => {}) as Foo` and `memo(function Foo() {})` define functions too."""
    for _ in range(4):
        if value is None:
            break
        if value.type in _JS_VALUE_WRAPPERS and value.named_children:
            value = value.named_children[0]
        elif (
            value.type == "call_expression"
            and _text(value.child_by_field_name("function")) in _JS_FUNCTION_WRAPPERS
        ):
            args = value.child_by_field_name("arguments")
            first = args.named_children[0] if args and args.named_children else None
            if first is None or first.type not in _JS_FUNCTION_VALUES:
                break
            value = first
        else:
            break
    return value


def _jsdoc(node: Node) -> str | None:
    anchor = node
    while anchor.parent is not None and anchor.parent.type in _JS_WRAPPERS:
        anchor = anchor.parent
    prev = anchor.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    # Allow one blank line between the comment and the declaration.
    if prev.end_point.row < anchor.start_point.row - 2:
        return None
    text = _text(prev)
    if not text.startswith("/**"):
        return None
    lines = text[3:].removesuffix("*/").splitlines()
    return _clip("\n".join(line.strip().removeprefix("*").strip() for line in lines))


def _first_line(src: bytes, node: Node) -> str:
    end = src.find(b"\n", node.start_byte, node.end_byte)
    return _between(src, node.start_byte, end if end != -1 else node.end_byte)


def _parse_js(b_args: dict, src: bytes, root: Node) -> _Builder:
    b = _Builder(sep=":", module_doc=None, **b_args)
    stack: list[tuple[Node, int, bool]] = [(root, 0, False)]
    while stack:
        node, parent, in_class = stack.pop()
        t = node.type
        children_parent = parent
        children_in_class = False
        children: list[Node] = list(node.named_children)

        if t in _JS_CLASS_DECLS and node.child_by_field_name("name") is not None:
            body = node.child_by_field_name("body")
            children_parent = b.add(
                "class",
                _text(node.child_by_field_name("name")),
                parent,
                node,
                _between(
                    src, node.start_byte, body.start_byte if body else node.end_byte
                ),
                _jsdoc(node),
            )
            children = list(body.named_children) if body else []
            children_in_class = True
        elif t in ("function_declaration", "generator_function_declaration"):
            body = node.child_by_field_name("body")
            children_parent = b.add(
                "function",
                _text(node.child_by_field_name("name")),
                parent,
                node,
                _between(
                    src, node.start_byte, body.start_byte if body else node.end_byte
                ),
                _jsdoc(node),
            )
        elif t in ("method_definition", "abstract_method_signature") and in_class:
            body = node.child_by_field_name("body")
            children_parent = b.add(
                "method",
                _text(node.child_by_field_name("name")),
                parent,
                node,
                _between(
                    src, node.start_byte, body.start_byte if body else node.end_byte
                ),
                _jsdoc(node),
            )
        elif t in ("public_field_definition", "field_definition") and in_class:
            name = node.child_by_field_name("name") or node.child_by_field_name(
                "property"
            )
            value = node.child_by_field_name("value")
            if (
                name is not None
                and value is not None
                and value.type in _JS_FUNCTION_VALUES
            ):
                body = value.child_by_field_name("body")
                children_parent = b.add(
                    "method",
                    _text(name),
                    parent,
                    node,
                    _between(
                        src, node.start_byte, body.start_byte if body else node.end_byte
                    ),
                    _jsdoc(node),
                )
        elif t == "variable_declarator":
            name = node.child_by_field_name("name")
            value = _js_unwrap(node.child_by_field_name("value"))
            if name is not None and name.type == "identifier" and value is not None:
                if value.type in _JS_FUNCTION_VALUES:
                    body = value.child_by_field_name("body")
                    decl = node.parent if node.parent is not None else node
                    children_parent = b.add(
                        "function",
                        _text(name),
                        parent,
                        node,
                        _between(
                            src,
                            decl.start_byte,
                            body.start_byte if body else node.end_byte,
                        ),
                        _jsdoc(node),
                    )
                    children = list(value.named_children)
                elif value.type == "class":
                    body = value.child_by_field_name("body")
                    children_parent = b.add(
                        "class",
                        _text(name),
                        parent,
                        node,
                        _between(
                            src,
                            node.start_byte,
                            body.start_byte if body else node.end_byte,
                        ),
                        _jsdoc(node),
                    )
                    children = list(body.named_children) if body else []
                    children_in_class = True
        elif t == "assignment_expression":
            # module.exports.foo = function () {}, Foo.prototype.bar = () => {}
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if (
                left is not None
                and left.type == "member_expression"
                and right is not None
                and right.type in _JS_FUNCTION_VALUES
            ):
                prop = left.child_by_field_name("property")
                body = right.child_by_field_name("body")
                children_parent = b.add(
                    "function",
                    _text(prop),
                    parent,
                    node,
                    _between(
                        src, node.start_byte, body.start_byte if body else node.end_byte
                    ),
                    _jsdoc(node.parent or node),
                    chain_name=_text(left),
                )
                children = list(right.named_children)
        elif t in _TS_TYPE_DECLS:
            b.add(
                _TS_TYPE_DECLS[t],
                _text(node.child_by_field_name("name")),
                parent,
                node,
                _first_line(src, node),
                _jsdoc(node),
            )
            continue

        for child in reversed(children):
            stack.append((child, children_parent, children_in_class))
    return b


def parse_source(path: str, language: str, src: bytes) -> ParsedFile:
    tree = _parser(language).parse(src)
    total_lines = src.count(b"\n") + (0 if src.endswith(b"\n") else 1)
    args = {"module": module_name(path, language), "total_lines": total_lines}
    if language == "python":
        b = _parse_python(args, src, tree.root_node)
    else:
        b = _parse_js(args, src, tree.root_node)
    facts = extract_facts(path, language, tree.root_node, b.symbols)
    return ParsedFile(
        symbols=b.symbols, has_error=tree.root_node.has_error, facts=facts
    )
