from app.answer.schema import (
    AnswerReference,
    AnswerSentence,
    Evidence,
    ModelAnswer,
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

    references = []
    seen = set()
    for r in model.references:
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
    return sentences, references
