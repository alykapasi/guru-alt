"""Where each evaluation area's examples come from, and what is frozen of them (S59).

Each area names a model decision the founder can judge from their own use: the band a grader
gave an answer, the diagnosis on a failed one, what the practice gate decided a reply did, and
the coverage label a grounded reply carried. An adapter finds candidate sources in eval-eligible
accounts that have not been copied yet, and freezes each into the inputs a labeller sees (the
snapshot) and the decision being judged — both as they stand at the moment of drawing.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.conversation_evidence import TurnIntent
from app.learning.diagnosis import FailureKind
from app.models.chat import Conversation, Message
from app.models.evaluation import EvalExample, IntentRecord
from app.models.grading import GradingSnapshot
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.source import Chunk
from app.rag.coverage import coverage

GRADING_BANDS = ("fail", "partial", "pass")
SUPPORT = ("supported", "partly", "unsupported")

# The label each area takes: one key, and the values it may hold.
VOCABULARY: dict[str, tuple[str, tuple[str, ...]]] = {
    "grading": ("band", GRADING_BANDS),
    "diagnosis": ("kind", tuple(k.value for k in FailureKind)),
    "intent": ("intent", tuple(i.value for i in TurnIntent)),
    # Not the coverage label itself: that is a rule over what was offered and cited, so a label
    # in its own vocabulary could only echo it. The founder judges whether the reply is
    # supported by the passages it cited; the report reads that against the coverage label.
    "source_support": ("support", SUPPORT),
}


def grading_band(score: float) -> str:
    """The report's convention, never the product's: fail < 0.4 ≤ partial < 0.8 ≤ pass."""
    if score < 0.4:
        return "fail"
    if score < 0.8:
        return "partial"
    return "pass"


@dataclass(frozen=True)
class Frozen:
    """One candidate, frozen: what to copy and, for stratified draws, which stratum it is in."""

    learner_id: uuid.UUID
    source_kind: str
    source_id: uuid.UUID
    snapshot: dict
    decision: dict
    stratum: str | None = None


def _not_copied(area: str, source_kind: str, source_id) -> ColumnElement[bool]:
    return ~exists().where(
        EvalExample.area == area,
        EvalExample.source_kind == source_kind,
        EvalExample.source_id == source_id,
    )


def _eligible(learner_id_column) -> ColumnElement[bool]:
    return exists().where(Learner.id == learner_id_column, Learner.eval_eligible.is_(True))


# --- grading and diagnosis --------------------------------------------------------------------


def _graded() -> ColumnElement[bool]:
    grading = LearningEvent.payload["grading"]
    return and_(
        LearningEvent.event_type == "observation",
        grading.isnot(None),
        grading["grader"].astext != "self",
        LearningEvent.payload["response"].isnot(None),
    )


async def _snapshots(
    session: AsyncSession, learner_id: uuid.UUID, hashes: Sequence[str | None]
) -> dict[str, dict]:
    wanted = [h for h in hashes if h]
    if not wanted:
        return {}
    rows = await session.scalars(
        select(GradingSnapshot).where(
            GradingSnapshot.learner_id == learner_id, GradingSnapshot.sha256.in_(wanted)
        )
    )
    return {s.sha256: s.content for s in rows}


def _response_text(response: object) -> str:
    if isinstance(response, dict):
        return str(response.get("text") or response.get("choice") or response)
    return str(response)


def _grading_query(limit: int) -> Select:
    attempt = func.coalesce(LearningEvent.attempt_id, LearningEvent.id)
    return (
        select(LearningEvent)
        .where(
            _graded(),
            _eligible(LearningEvent.learner_id),
            _not_copied("grading", "attempt", attempt),
        )
        .order_by(func.random())
        .limit(limit)
    )


async def grading_candidates(session: AsyncSession, limit: int) -> list[Frozen]:
    seen: set[uuid.UUID] = set()
    out: list[Frozen] = []
    for event in (await session.scalars(_grading_query(limit))).all():
        attempt = event.attempt_id or event.id
        if attempt in seen:
            continue
        seen.add(attempt)
        grading = event.payload["grading"]
        found = await _snapshots(
            session, event.learner_id, [grading["item"], grading.get("rubric")]
        )
        item = found.get(grading["item"])
        if item is None:
            continue
        rubric = found.get(grading.get("rubric") or "")
        score = float(event.payload.get("item_score", event.payload["score"]))
        band = grading_band(score)
        out.append(
            Frozen(
                learner_id=event.learner_id,
                source_kind="attempt",
                source_id=attempt,
                snapshot={
                    "stem": item["stem"],
                    "answer": _response_text(event.payload["response"]),
                    "criteria": (rubric or {}).get("criteria"),
                },
                decision={
                    "score": score,
                    "band": band,
                    "grader": grading["grader"],
                    "model": grading.get("model"),
                },
                stratum=band,
            )
        )
    return out


async def diagnosis_candidates(session: AsyncSession, limit: int) -> list[Frozen]:
    kind = LearningEvent.payload["diagnosis"]["kind"].astext
    query = (
        select(LearningEvent)
        .where(
            _graded(),
            kind.isnot(None),
            kind != FailureKind.NONE.value,
            _eligible(LearningEvent.learner_id),
            _not_copied("diagnosis", "event", LearningEvent.id),
        )
        .order_by(func.random())
        .limit(limit)
    )
    out: list[Frozen] = []
    for event in (await session.scalars(query)).all():
        grading = event.payload["grading"]
        found = await _snapshots(session, event.learner_id, [grading["item"]])
        item = found.get(grading["item"])
        if item is None:
            continue
        component = next(
            (c["name"] for c in item.get("components", []) if c["kc_id"] == str(event.kc_id)),
            None,
        )
        diagnosis = event.payload["diagnosis"]
        out.append(
            Frozen(
                learner_id=event.learner_id,
                source_kind="event",
                source_id=event.id,
                snapshot={
                    "stem": item["stem"],
                    "answer": _response_text(event.payload["response"]),
                    "component": component,
                },
                decision={
                    "kind": diagnosis["kind"],
                    "confidence": diagnosis.get("confidence"),
                    "evidence": diagnosis.get("evidence"),
                },
            )
        )
    return out


# --- intent -----------------------------------------------------------------------------------


async def intent_candidates(session: AsyncSession, limit: int) -> list[Frozen]:
    query = (
        select(IntentRecord)
        .where(
            _eligible(IntentRecord.learner_id),
            _not_copied("intent", "intent", IntentRecord.id),
        )
        .order_by(func.random())
        .limit(limit)
    )
    return [
        Frozen(
            learner_id=r.learner_id,
            source_kind="intent",
            source_id=r.id,
            snapshot={"stem": r.stem, "message": r.message},
            decision={"intent": r.intent},
        )
        for r in (await session.scalars(query)).all()
    ]


# --- source support ---------------------------------------------------------------------------


async def source_support_candidates(session: AsyncSession, limit: int) -> list[Frozen]:
    query = (
        select(Message, Conversation.learner_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.role == "assistant",
            Message.grounding_count.isnot(None),
            Message.admin_actor_id.is_(None),
            _eligible(Conversation.learner_id),
            _not_copied("source_support", "message", Message.id),
        )
        .order_by(func.random())
        .limit(limit)
    )
    out: list[Frozen] = []
    for message, learner_id in (await session.execute(query)).all():
        question = await session.scalar(
            select(Message.content)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.role == "user",
                Message.created_at <= message.created_at,
                Message.id != message.id,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        chunk_ids = [uuid.UUID(c["chunk_id"]) for c in message.citations if c.get("chunk_id")]
        texts: dict[uuid.UUID, str] = {}
        if chunk_ids:
            found = await session.execute(
                select(Chunk.id, Chunk.text).where(Chunk.id.in_(chunk_ids))
            )
            texts = {chunk_id: text for chunk_id, text in found.all()}
        passages = [
            {
                "marker": c.get("marker"),
                "text": texts.get(uuid.UUID(c["chunk_id"]), "(passage no longer available)")
                if c.get("chunk_id")
                else "(passage no longer available)",
            }
            for c in message.citations
        ]
        label = coverage(message.grounding_count, message.citations)
        out.append(
            Frozen(
                learner_id=learner_id,
                source_kind="message",
                source_id=message.id,
                snapshot={
                    "question": question or "",
                    "reply": message.content,
                    "passages": passages,
                },
                decision={
                    "coverage": label.value if label is not None else None,
                    "offered": message.grounding_count,
                },
            )
        )
    return out


ADAPTERS: dict[str, Callable[[AsyncSession, int], Awaitable[list[Frozen]]]] = {
    "grading": grading_candidates,
    "diagnosis": diagnosis_candidates,
    "intent": intent_candidates,
    "source_support": source_support_candidates,
}


async def available(session: AsyncSession, area: str) -> int:
    """How many sources could still be drawn — counted, not frozen."""
    if area == "grading":
        attempt = func.coalesce(LearningEvent.attempt_id, LearningEvent.id)
        query = select(func.count(func.distinct(attempt))).where(
            _graded(),
            _eligible(LearningEvent.learner_id),
            _not_copied("grading", "attempt", attempt),
        )
    elif area == "diagnosis":
        kind = LearningEvent.payload["diagnosis"]["kind"].astext
        query = select(func.count()).where(
            _graded(),
            kind.isnot(None),
            kind != FailureKind.NONE.value,
            _eligible(LearningEvent.learner_id),
            _not_copied("diagnosis", "event", LearningEvent.id),
        )
    elif area == "intent":
        query = select(func.count()).where(
            _eligible(IntentRecord.learner_id),
            _not_copied("intent", "intent", IntentRecord.id),
        )
    else:
        query = (
            select(func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.role == "assistant",
                Message.grounding_count.isnot(None),
                Message.admin_actor_id.is_(None),
                _eligible(Conversation.learner_id),
                _not_copied("source_support", "message", Message.id),
            )
        )
    return int(await session.scalar(query) or 0)
