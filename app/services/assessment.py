"""Assessment items: creation, retrieval, and the answer→grade→trace loop.

``answer_item`` is the heart of the adaptive loop: grade the response, fold the result
into the tracer for every tagged KC, and commit grade + state updates + the event log in
one transaction so an interaction is recorded atomically. It also cheaply revises any
lesson plan touching the graded KCs (``lesson_plan.revise_plan`` — DB-only, no LLM call) so
a plan's step statuses/reviews stay current without waiting for a manual regenerate.

Atomic is not the same as *once*. A submission may also carry an ``attempt_id``, which makes
it idempotent: the recorded grade is returned unchanged rather than the answer being graded
and traced a second time. See ``answer_item`` and ``_recorded_attempt``.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.learning import mastery, rubric_grading
from app.learning.grading import GradeResult, NotAutoGradable, auto_grade, grade_flashcard
from app.learning.item_presentation import public_presentation
from app.learning.mastery import Observation
from app.learning.rubric_grading import GRADING_ROLE
from app.llm import LLMClient
from app.models.assessment import (
    AUTO_GRADABLE,
    RUBRIC_GRADABLE,
    SELF_GRADABLE,
    Item,
    ItemKC,
    ItemType,
)
from app.models.learning import LearnerKCState, LearningEvent
from app.schemas.assessment import AnswerSubmit, ItemCreate, ItemKCRead, ItemRead
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.llm_log import log_llm_call


async def create_item(session: AsyncSession, data: ItemCreate) -> Item:
    item = Item(
        item_type=data.item_type,
        stem=data.stem,
        answer_key=data.answer_key,
        difficulty=data.difficulty,
        rubric_id=data.rubric_id,
        kc_links=[ItemKC(kc_id=k.kc_id, weight=k.weight) for k in data.kcs],
    )
    session.add(item)
    await session.commit()
    return item


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> Item | None:
    return await session.scalar(
        select(Item)
        .where(Item.id == item_id)
        .options(selectinload(Item.kc_links), selectinload(Item.rubric))
    )


async def find_item_for_kc(
    session: AsyncSession, kc_id: uuid.UUID, *, item_type: ItemType | None = None
) -> Item | None:
    """The oldest bank item assessing ``kc_id``, if any — reuse before generating a new one.

    ``item_type``, if given, restricts the search to that type (e.g. the session runner
    preferring a flashcard for a review step) — ``None`` matches any type, the prior behavior.
    """
    stmt = select(Item).join(ItemKC, ItemKC.item_id == Item.id).where(ItemKC.kc_id == kc_id)
    if item_type is not None:
        stmt = stmt.where(Item.item_type == item_type)
    return await session.scalar(
        stmt.options(selectinload(Item.kc_links), selectinload(Item.rubric))
        .order_by(Item.created_at)
        .limit(1)
    )


def item_to_read(item: Item) -> ItemRead:
    """Project an item for the learner — without leaking its answer key."""
    item_type = ItemType(item.item_type)
    return ItemRead(
        id=item.id,
        item_type=item_type,
        stem=item.stem,
        difficulty=item.difficulty,
        rubric_id=item.rubric_id,
        kcs=[ItemKCRead(kc_id=link.kc_id, weight=link.weight) for link in item.kc_links],
        presentation=public_presentation(item_type, item.answer_key),
    )


async def answer_item(
    session: AsyncSession,
    learner_id: uuid.UUID,
    item: Item,
    submission: AnswerSubmit,
    *,
    llm: LLMClient,
) -> tuple[GradeResult, Sequence[LearnerKCState]]:
    """Grade an item — deterministically or by rubric — and trace the result atomically.

    Caller ensures the item is gradable. Objective types grade with no model call; open
    types call the SMART model (logged) before any DB write, so a grading failure leaves
    the transaction untouched.

    If the submission carries an ``attempt_id``, it is an idempotency key: an attempt already
    recorded under that id is returned as-is, without re-grading (no second model call) and
    without a second mastery update. The check is cheap but racy on its own, so the unique
    index on (learner, attempt, KC) is what actually decides a tie — a concurrent duplicate
    loses at commit and replays the winner's grade instead of raising.
    """
    if submission.attempt_id is not None:
        recorded = await _recorded_attempt(session, learner_id, item, submission.attempt_id)
        if recorded is not None:
            return recorded
    result = await _grade(session, learner_id, item, submission, llm=llm)
    observation = Observation(
        learner_id=learner_id,
        kc_weights={link.kc_id: link.weight for link in item.kc_links},
        score=result.score,
        difficulty=item.difficulty,
        item_id=item.id,
        response=submission.response,
        latency_ms=submission.latency_ms,
        hints_used=submission.hints_used,
        attempt_id=submission.attempt_id,
        correct=result.correct,
        detail=result.detail,
    )
    states = await mastery.DEFAULT_TRACER.update(session, observation)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        recorded = (
            await _recorded_attempt(session, learner_id, item, submission.attempt_id)
            if submission.attempt_id is not None
            else None
        )
        if recorded is None:
            raise  # not the idempotency index — a real constraint violation
        return recorded
    # Mastery is ground truth and must land regardless; the plan is a derived projection, so
    # this revises *after* that commit rather than folding it into the same transaction.
    for subject_id in await knowledge_svc.subjects_for_kcs(session, observation.kc_weights):
        await lesson_plan_svc.revise_plan(session, learner_id=learner_id, subject_id=subject_id)
    return result, states


async def _recorded_attempt(
    session: AsyncSession,
    learner_id: uuid.UUID,
    item: Item,
    attempt_id: uuid.UUID,
) -> tuple[GradeResult, Sequence[LearnerKCState]] | None:
    """The grade already recorded under ``attempt_id``, or ``None`` if it is a new attempt.

    Rebuilt from the event log rather than a separate results table: ``record_observation``
    writes the verdict into every row of the attempt's per-KC fan-out, so any one of them
    reconstructs the response the first request got, and the states are simply read live.
    """
    event = await session.scalar(
        select(LearningEvent)
        .where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.attempt_id == attempt_id,
        )
        .limit(1)
    )
    if event is None:
        return None
    payload = event.payload
    result = GradeResult(
        score=payload["score"],
        correct=bool(payload.get("correct", payload["score"] >= 1.0)),
        detail=payload.get("detail") or {},
    )
    kc_ids = [link.kc_id for link in item.kc_links]
    states = (
        await session.scalars(
            select(LearnerKCState).where(
                LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id.in_(kc_ids)
            )
        )
    ).all()
    by_kc = {state.kc_id: state for state in states}
    return result, [by_kc[kc_id] for kc_id in kc_ids if kc_id in by_kc]


async def _grade(
    session: AsyncSession,
    learner_id: uuid.UUID,
    item: Item,
    submission: AnswerSubmit,
    *,
    llm: LLMClient,
) -> GradeResult:
    """Route to deterministic or LLM rubric grading. Logs the call on the rubric path."""
    item_type = ItemType(item.item_type)
    if item_type in AUTO_GRADABLE:
        return auto_grade(item_type, item.answer_key or {}, submission.response)
    if item_type in SELF_GRADABLE:
        return grade_flashcard(submission.response)
    if item_type in RUBRIC_GRADABLE:
        result, usage = await rubric_grading.grade_open(
            llm, stem=item.stem, response=submission.response, rubric=item.rubric
        )
        if usage.total_tokens:  # an empty response short-circuits with no model call
            await log_llm_call(
                session,
                learner_id=learner_id,
                role=GRADING_ROLE.value,
                spec=llm.spec(GRADING_ROLE),
                usage=usage,
            )
        return result
    raise NotAutoGradable(f"{item_type} items have no grading path")
