from app.answer.schema import (
    AnswerReference,
    AnswerSentence,
    Citations,
    Evidence,
    FlowNode,
    ModelAnswer,
    ModelFlowStep,
    ModelReference,
)


def validate_answer(
    model: ModelAnswer, ledger: dict[str, Evidence], url_for
) -> tuple[list[AnswerSentence], list[AnswerReference]]:
    """Drop citations that are not in the ledger and keep line ranges inside
    the cited evidence. A fact left with no valid evidence becomes an
    interpretation. url_for(path, start, end) builds links server-side."""
    sentences = []
    for s in model.explanation:
        valid = [e for e in dict.fromkeys(s.evidence) if e in ledger]
        downgraded = s.kind == "fact" and not valid
        sentences.append(
            AnswerSentence(
                text=s.text,
                kind="interpretation" if downgraded else s.kind,
                evidence=valid,
                downgraded=downgraded,
            )
        )

    return sentences, validate_references(model.references, ledger, url_for)


def validate_references(
    refs: list[ModelReference], ledger: dict[str, Evidence], url_for
) -> list[AnswerReference]:
    references = []
    seen = set()
    for r in refs:
        ev = ledger.get(r.evidence)
        if ev is None:
            continue
        start = min(max(r.start_line, ev.start_line), ev.end_line)
        end = min(max(r.end_line, start), ev.end_line)
        if r.end_line < ev.start_line or r.start_line > ev.end_line:
            start, end = ev.start_line, ev.end_line
        key = (ev.path, start, end)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            AnswerReference(
                path=ev.path,
                symbol=ev.symbol,
                start_line=start,
                end_line=end,
                url=url_for(ev.path, start, end),
                evidence=ev.id,
                note=r.note,
                is_test=ev.is_test,
            )
        )
    return references


def validate_flow(
    steps: list[ModelFlowStep], ledger: dict[str, Evidence], url_for
) -> list[FlowNode]:
    """Build the call-flow tree from the model's flat list. Steps citing
    unknown evidence are dropped and their children move up to the nearest
    kept ancestor; cycles and unknown parents become roots."""
    by_step = {s.step: s for s in steps}
    nodes: dict[int, FlowNode] = {}
    for s in steps:
        ev = ledger.get(s.evidence)
        if ev is None or s.step in nodes:
            continue
        nodes[s.step] = FlowNode(
            symbol=s.symbol,
            path=ev.path,
            start_line=ev.start_line,
            end_line=ev.end_line,
            url=url_for(ev.path, ev.start_line, ev.end_line),
            evidence=ev.id,
            note=s.note,
        )

    def kept_parent(step: int) -> int | None:
        seen = {step}
        parent = by_step[step].parent
        while parent in by_step and parent not in seen:
            if parent in nodes:
                return parent
            seen.add(parent)
            parent = by_step[parent].parent
        return None

    roots = []
    for step, node in nodes.items():
        parent = kept_parent(step)
        # A parent that is also a descendant would make a cycle.
        if parent is not None and not _descends(nodes[parent], node):
            nodes[parent].children.append(node)
        else:
            roots.append(node)
    return roots


def _descends(node: FlowNode, ancestor: FlowNode) -> bool:
    return any(c is node or _descends(node, c) for c in ancestor.children)


def count_citations(model: ModelAnswer, ledger: dict[str, Evidence]) -> Citations:
    """Count every evidence id the model cited, before validation drops any."""
    ids = [e for s in model.explanation for e in s.evidence]
    ids += [r.evidence for r in model.references]
    ids += [r.evidence for r in getattr(model, "tests", [])]
    ids += [f.evidence for f in getattr(model, "flow", [])]
    return Citations(cited=len(ids), valid=sum(1 for e in ids if e in ledger))
