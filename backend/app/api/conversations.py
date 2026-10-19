import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from anthropic import APIError
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.baseline import answer_baseline
from app.agent.ledger import Ledger
from app.agent.llm import LLMUnavailable
from app.agent.navigator import AgentError, PriorTurn, navigate
from app.answer.schema import Answer, Evidence, Step
from app.api.search import _searchable
from app.core.db import SessionLocal, get_session
from app.models import Conversation, Message, MessageStatus, Repo
from app.search.text import SearchError

log = logging.getLogger(__name__)

router = APIRouter(tags=["conversations"])

Session = Annotated[AsyncSession, Depends(get_session)]
Mode = Literal["navigator", "baseline"]
TITLE_CHARS = 120


class MessageOut(BaseModel):
    id: int
    mode: str
    question: str
    status: str
    answer: Answer | None
    trace: list[Step]
    error: str | None
    created_at: datetime

    @classmethod
    def of(cls, m: Message) -> "MessageOut":
        return cls(
            id=m.id,
            mode=m.mode,
            question=m.question,
            status=m.status,
            answer=Answer.model_validate(m.answer) if m.answer else None,
            trace=[Step.model_validate(s) for s in m.trace or []],
            error=m.error,
            created_at=m.created_at,
        )


class ConversationOut(BaseModel):
    id: int
    repo_id: int
    title: str | None
    created_at: datetime
    messages: list[MessageOut] = []


class MessageIn(BaseModel):
    question: str
    mode: Mode = "navigator"


async def _messages(session: AsyncSession, cid: int) -> list[Message]:
    rows = await session.execute(
        select(Message).where(Message.conversation_id == cid).order_by(Message.id)
    )
    return list(rows.scalars())


async def _conversation(session: AsyncSession, cid: int) -> Conversation:
    conv = await session.get(Conversation, cid)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conv


def _out(conv: Conversation, messages: list[Message]) -> ConversationOut:
    return ConversationOut(
        id=conv.id,
        repo_id=conv.repo_id,
        title=conv.title,
        created_at=conv.created_at,
        messages=[MessageOut.of(m) for m in messages],
    )


@router.post("/repos/{repo_id}/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(repo_id: int, session: Session) -> ConversationOut:
    repo = await _searchable(session, repo_id)
    conv = Conversation(repo_id=repo.id)
    session.add(conv)
    await session.commit()
    await session.refresh(conv)
    return _out(conv, [])


@router.get("/repos/{repo_id}/conversations")
async def list_conversations(repo_id: int, session: Session) -> list[ConversationOut]:
    rows = await session.execute(
        select(Conversation)
        .where(Conversation.repo_id == repo_id)
        .order_by(Conversation.created_at.desc())
    )
    return [_out(c, []) for c in rows.scalars()]


@router.get("/conversations/{cid}")
async def get_conversation(cid: int, session: Session) -> ConversationOut:
    conv = await _conversation(session, cid)
    return _out(conv, await _messages(session, cid))


def _sse(event: str, data: Any) -> str:
    if isinstance(data, BaseModel):
        payload = data.model_dump_json()
    else:
        payload = json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


def _history(messages: list[Message]) -> tuple[list[PriorTurn], list[Evidence]]:
    done = [m for m in messages if m.status == MessageStatus.DONE]
    turns = [PriorTurn(question=m.question, mode=m.mode, answer=m.answer) for m in done]
    # Each navigator turn's ledger starts from the previous one, so the last
    # holds every evidence id cited so far.
    last = next((m for m in reversed(done) if m.mode == "navigator"), None)
    prior = [Evidence.model_validate(e) for e in (last.ledger if last else [])]
    return turns, prior


async def run_turn(
    session: AsyncSession, repo: Repo, conv_id: int, question: str, mode: Mode
) -> AsyncIterator[tuple[str, Any]]:
    """Ask one question in a conversation, storing the message as it goes.
    Yields ("step", Step), then ("answer", Answer) or ("error", {message})."""
    history, prior = _history(await _messages(session, conv_id))
    msg = Message(
        conversation_id=conv_id,
        mode=mode,
        question=question,
        status=MessageStatus.RUNNING,
    )
    session.add(msg)
    conv = await session.get(Conversation, conv_id)
    if conv is not None and not conv.title:
        conv.title = question[:TITLE_CHARS]
    await session.commit()
    msg_id = msg.id
    trace: list[Step] = []
    ledgers: list[Ledger] = []
    finished = False

    async def finish(**fields) -> None:
        nonlocal finished
        finished = True
        # A fresh session, so a failed query in the turn cannot block the write
        # and the caller's loaded objects stay usable.
        async with SessionLocal() as s:
            row = await s.get(Message, msg_id)
            for k, v in fields.items():
                setattr(row, k, v)
            row.trace = [st.model_dump() for st in trace]
            await s.commit()

    try:
        if mode == "baseline":
            step = Step(
                index=1,
                tool="semantic_search",
                input={"query": question},
                label="Retrieving code chunks by semantic search",
                summary="",
            )
            yield "step", step
            answer = await answer_baseline(session, repo, question)
            step.summary = f"{len(answer.evidence)} code chunks"
            step.evidence = [e.id for e in answer.evidence]
            trace.append(step)
            answer.trace = trace
            await finish(
                status=MessageStatus.DONE,
                answer=answer.model_dump(),
                ledger=[e.model_dump() for e in answer.evidence],
            )
            yield "answer", answer
            return

        async for event in navigate(
            session, repo, question, history, prior, ledger_out=ledgers
        ):
            if event.kind == "step":
                trace.append(event.data)
                yield "step", event.data
            else:
                ledger = (
                    [e.model_dump() for e in ledgers[0].entries.values()]
                    if ledgers
                    else []
                )
                await finish(
                    status=MessageStatus.DONE,
                    answer=event.data.model_dump(),
                    ledger=ledger,
                )
                yield "answer", event.data
                return
    except (LLMUnavailable, SearchError, AgentError, RuntimeError) as exc:
        await finish(status=MessageStatus.FAILED, error=str(exc))
        yield "error", {"message": str(exc)}
    except APIError as exc:
        log.warning("Claude API error: %s", exc)
        message = f"The Claude API returned an error: {getattr(exc, 'message', exc)}"
        await finish(status=MessageStatus.FAILED, error=message)
        yield "error", {"message": message}
    except Exception:
        log.exception("Turn failed")
        message = "Something went wrong while answering. Check the backend log."
        await finish(status=MessageStatus.FAILED, error=message)
        yield "error", {"message": message}
    finally:
        if not finished:
            # The client went away mid-turn; record that in a fresh session.
            async with SessionLocal() as s:
                row = await s.get(Message, msg_id)
                if row is not None and row.status == MessageStatus.RUNNING:
                    row.status = MessageStatus.FAILED
                    row.error = "Stopped before an answer."
                    row.trace = [st.model_dump() for st in trace]
                    await asyncio.shield(s.commit())


@router.post("/conversations/{cid}/messages")
async def post_message(
    cid: int, body: MessageIn, session: Session
) -> StreamingResponse:
    """Ask a question. Streams Server-Sent Events: `step` for each tool call,
    then `answer` with the validated answer, or `error`."""
    conv = await _conversation(session, cid)
    await _searchable(session, conv.repo_id)
    question = body.question.strip()
    if not question:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Question is empty")
    repo_id = conv.repo_id

    async def stream() -> AsyncIterator[str]:
        async with SessionLocal() as s:
            repo = await s.get(Repo, repo_id)
            async for kind, data in run_turn(s, repo, cid, question, body.mode):
                yield _sse(kind, data)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
