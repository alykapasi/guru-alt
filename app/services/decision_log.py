"""The one place a Jev answer becomes a record (S82).

Same two rules as ``app.services.llm_log``: the row is written on its own session so it
survives the turn rolling back, and a structured log line is emitted first and a failed write is
swallowed — a decision exists to save a model call, and losing its audit row must never cost the
learner their turn.
"""

from __future__ import annotations

import uuid

import structlog

from app.learning.turn_read import TurnRead
from app.llm.decisions import Answer, ChoiceAnswer, DecisionFailure, DecisionResponse
from app.llm.pricing import price_usd
from app.llm.types import Usage
from app.models.decision import DecisionCall
from app.services.llm_log import accounting_session

log = structlog.get_logger(__name__)


async def record_decision(
    *,
    read: TurnRead,
    question: str,
    mode: str,
    outcome: Answer | DecisionFailure,
    used: bool,
    baseline_intent: str | None = None,
    baseline_score: float | None = None,
    attempt_id: uuid.UUID | None = None,
) -> None:
    """Record one question's answer (or failure) beside what today's path decided."""
    if isinstance(outcome, DecisionFailure):
        status, answer, probabilities, confidence = outcome.kind.value, None, None, None
    elif isinstance(outcome, ChoiceAnswer):
        status, answer = "ok", outcome.label
        probabilities, confidence = dict(outcome.probabilities), outcome.confidence
    else:
        status, answer, probabilities, confidence = "ok", None, {"yes": outcome.probability}, None

    response = read.completed()
    finished = response if isinstance(response, DecisionResponse) else None
    tokens = finished.input_tokens if finished is not None else None
    model = finished.model if finished is not None else read.client.model
    cost = (
        price_usd(read.client.provider, model, Usage(input_tokens=tokens, output_tokens=0))
        if tokens is not None
        else None
    )
    latency = finished.latency_ms if finished is not None else None

    log.info(
        "decision.call",
        question=question,
        mode=mode,
        status=status,
        answer=answer,
        confidence=confidence,
        probability=probabilities.get("yes") if probabilities and answer is None else None,
        used=used,
        latency_ms=latency,
        cost_usd=cost,
    )
    try:
        async with accounting_session() as session:
            session.add(
                DecisionCall(
                    request_id=read.request_id,
                    learner_id=read.context.learner_id,
                    conversation_id=read.context.conversation_id,
                    item_id=read.context.item_id,
                    attempt_id=attempt_id,
                    question=question,
                    mode=mode,
                    provider=read.client.provider,
                    model=model,
                    status=status,
                    answer=answer,
                    probabilities=probabilities,
                    confidence=confidence,
                    baseline_intent=baseline_intent,
                    baseline_score=baseline_score,
                    used=used,
                    input_tokens=tokens,
                    cost_usd=cost,
                    latency_ms=latency,
                )
            )
            await session.commit()
    except Exception as exc:  # bookkeeping must not fail the turn it accounts for
        log.error("decision.call_not_recorded", question=question, error=str(exc))
