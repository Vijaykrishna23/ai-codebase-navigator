from typing import Literal

from pydantic import BaseModel

SentenceKind = Literal["fact", "interpretation"]


# What the model returns. Kept flat: structured outputs work best with simple schemas.


class ModelSentence(BaseModel):
    text: str
    kind: SentenceKind
    evidence: list[str]


class ModelReference(BaseModel):
    evidence: str
    start_line: int
    end_line: int
    note: str


class ModelAnswer(BaseModel):
    summary: str
    explanation: list[ModelSentence]
    references: list[ModelReference]
    gaps: list[str]


# What the API returns, after validation against the evidence ledger.


class Evidence(BaseModel):
    id: str
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    url: str
    content: str
    score: float | None = None
    is_test: bool = False


class AnswerSentence(BaseModel):
    text: str
    kind: SentenceKind
    evidence: list[str]
    # True when the model called it a fact but cited nothing valid.
    downgraded: bool = False


class AnswerReference(BaseModel):
    path: str
    symbol: str | None
    start_line: int
    end_line: int
    url: str
    evidence: str
    note: str
    is_test: bool = False


class Usage(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Answer(BaseModel):
    mode: Literal["baseline", "navigator"]
    question: str
    summary: str
    explanation: list[AnswerSentence]
    references: list[AnswerReference]
    tests: list[AnswerReference]
    gaps: list[str]
    evidence: list[Evidence]
    usage: Usage
