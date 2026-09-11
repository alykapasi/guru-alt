"""Assessment items: creation, retrieval, and the answer→grade→trace loop.

``answer_item`` is the heart of the adaptive loop: grade the response, fold the result
into the tracer for every tagged KC, and commit grade + state updates + the event log in
one transaction so an interaction is recorded atomically. It also cheaply revises any
lesson plan touching the graded KCs (``lesson_plan.revise_plan`` — DB-only, no LLM call) so
a plan's step statuses/reviews stay current without waiting for a manual regenerate.

Atomic is not the same as *once*. A submission may also carry an ``attempt_id``, which makes
it idempotent: the recorded grade is returned unchanged rather than the answer being graded
and traced a second time. See ``answer_item`` and ``_recorded_attempt``.

The plan revision is best-effort in the strict sense: a failure there must never report a
committed grade as failed. It records the debt on the plan instead, and the next read of that
plan pays it (``_revise_plans``).
"""

import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.learning import mastery, rubric_grading
from app.learning.diagnosis import Diagnosis
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
    ItemOrigin,
    ItemType,
)
from app.models.knowledge import KC
from app.models.learning import LearnerKCState, LearningEvent
from app.schemas.assessment import AnswerSubmit, ItemCreate, ItemKCRead, ItemRead
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.llm_log import log_llm_call

log = structlog.get_logger(__name__)


async def create_item(
    session: AsyncSession, data: ItemCreate, *, author_learner_id: uuid.UUID | None = None
) -> Item:
    """Persist one item. With an ``author_learner_id`` it is that learner's alone (S33).

    ``None`` means the platform's own generators wrote it, which is the only provenance that
    puts an item in the shared bank. There is deliberately no way to promote a learner's item
    into that bank: nothing in the system can yet establish who is entitled to author an
    assessment other people are graded against (S25, and real auth in Phase 10).
    """
    item = Item(
        item_type=data.item_type,
        stem=data.stem,
        answer_key=data.answer_key,
        difficulty=data.difficulty,
        rubric_id=data.rubric_id,
        origin=ItemOrigin.LEARNER if author_learner_id else ItemOrigin.GENERATED,
        author_learner_id=author_learner_id,
        kc_links=[ItemKC(kc_id=k.kc_id, weight=k.weight) for k in data.kcs],
    )
    session.add(item)
    await session.commit()
    return item


def _assessable_by(learner_id: uuid.UUID):
    """The items ``learner_id`` may be assessed with: the shared bank, plus their own.

    One predicate, used by every read path, so a new one cannot forget it.
    """
    return or_(Item.origin == ItemOrigin.GENERATED, Item.author_learner_id == learner_id)


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> Item | None:
    """One item by id, unfiltered — for internal callers that already know it is the
    learner's. Anything reached from a request should use :func:`get_item_for`."""
    return await session.scalar(
        select(Item)
        .where(Item.id == item_id)
        .options(selectinload(Item.kc_links), selectinload(Item.rubric))
    )


async def get_item_for(
    session: AsyncSession, item_id: uuid.UUID, *, learner_id: uuid.UUID
) -> Item | None:
    """One item, if this learner may see it — otherwise ``None``, indistinguishable from
    absent. Another learner's private item should not be readable *or* answerable: the stem
    and, for MCQs, the choices are exposed (S54), and answering it would write a mastery
    observation from a question nobody vouched for."""
    return await session.scalar(
        select(Item)
        .where(Item.id == item_id, _assessable_by(learner_id))
        .options(selectinload(Item.kc_links), selectinload(Item.rubric))
    )


async def find_item_for_kc(
    session: AsyncSession,
    kc_id: uuid.UUID,
    *,
    learner_id: uuid.UUID,
    item_type: ItemType | None = None,
    target_difficulty: float | None = None,
) -> Item | None:
    """The freshest bank item assessing ``kc_id``, if any — reuse before generating a new one.

    Freshest means *least recently answered by this learner*, with never-answered first. This
    used to be ``ORDER BY created_at``, which is stable: a learner practising a KC twice got
    the same question twice, and practising it ten times got it ten times. What that measures
    after the first attempt is recall of one question, not the component — and because every
    attempt still updated mastery, repeating the answer the learner had just been told drove
    the estimate up. A KC's bank is small, so ordering by exposure is what makes a second
    visit a different problem (S14).

    ``item_type``, if given, restricts the search to that type (e.g. the session runner
    preferring a flashcard for a review step) — ``None`` matches any type, the prior behavior.

    ``target_difficulty`` (S12) picks *which* fresh item, and deliberately only breaks ties
    that exposure has already left open. Fit does not outrank freshness: a question pitched
    exactly right that the learner answered an hour ago still measures memory of that question.
    Ordering it the other way round would have undone S14 the week after it landed.

    Until generation started recording a difficulty this changed nothing — every item in the
    bank sat at the 0.0 default, so every candidate was equidistant from any target and the
    ``created_at`` tiebreak carried the order exactly as before.

    Scoped to what ``learner_id`` may be assessed with (S33): reuse used to pick up anything
    tagged to the KC, so a question and answer key another learner had written became this
    learner's practice — and the mastery observation it produced was traced to it.
    """
    # Correlated per candidate item, which is cheap because a KC's bank is small and
    # ix_learning_events_learner_item makes each lookup an index probe.
    last_answered = (
        select(func.max(LearningEvent.created_at))
        .where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.event_type == "observation",
            LearningEvent.payload["item_id"].astext == cast(Item.id, String),
        )
        .correlate(Item)
        .scalar_subquery()
    )
    stmt = (
        select(Item)
        .join(ItemKC, ItemKC.item_id == Item.id)
        .where(ItemKC.kc_id == kc_id, _assessable_by(learner_id))
    )
    if item_type is not None:
        stmt = stmt.where(Item.item_type == item_type)
    order = [last_answered.asc().nullsfirst()]
    if target_difficulty is not None:
        order.append(func.abs(Item.difficulty - target_difficulty))
    # created_at last so the order is total: two equally fresh, equally well-fitted items
    # still come back in a fixed order rather than whatever the scan happened to produce.
    order.append(Item.created_at)
    return await session.scalar(
        stmt.options(selectinload(Item.kc_links), selectinload(Item.rubric))
        .order_by(*order)
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
        origin=item.origin,
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

    Evidence is discounted when the attempt was assisted — hints reported by the caller, plus
    earlier attempts at this same item in this sitting, counted here rather than trusted from
    the request. See :mod:`app.learning.assistance`.
    """
    kc_weights = {link.kc_id: link.weight for link in item.kc_links}
    if submission.attempt_id is not None:
        replayed = await _recorded_grade(session, learner_id, submission.attempt_id)
        if replayed is not None:
            # A retry is also the cheapest chance to finish work the first attempt could not:
            # if that request's plan revision failed, this one brings the plan up to date.
            await _revise_plans(session, learner_id, kc_weights)
            return replayed, await _states_for(session, learner_id, kc_weights)
    result = await _grade(session, learner_id, item, submission, llm=llm)
    observation = Observation(
        learner_id=learner_id,
        kc_weights=kc_weights,
        score=result.score,
        kc_scores=result.component_scores or None,
        kc_diagnoses=(
            {kc_id: d.model_dump(mode="json") for kc_id, d in result.diagnoses.items()} or None
        ),
        difficulty=item.difficulty,
        item_id=item.id,
        response=submission.response,
        latency_ms=submission.latency_ms,
        hints_used=submission.hints_used,
        # Counted server-side rather than trusted from the client: it is the learner's own
        # history that decides whether this is a fresh demonstration or a re-run.
        prior_attempts=await mastery.recent_attempts_at_item(session, learner_id, item.id),
        attempt_id=submission.attempt_id,
        correct=result.correct,
        detail=result.detail,
    )
    await mastery.DEFAULT_TRACER.update(session, observation)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        replayed = (
            await _recorded_grade(session, learner_id, submission.attempt_id)
            if submission.attempt_id is not None
            else None
        )
        if replayed is None:
            raise  # not the idempotency index — a real constraint violation
        result = replayed
    # Mastery is ground truth and must land regardless; the plan is a derived projection, so
    # this revises *after* that commit rather than folding it into the same transaction.
    await _revise_plans(session, learner_id, kc_weights)
    # Read the states last, and always: a failed revision rolls the session back, which
    # expires whatever the tracer handed us, and an expired row cannot be serialized.
    return result, await _states_for(session, learner_id, kc_weights)


async def _states_for(
    session: AsyncSession, learner_id: uuid.UUID, kc_weights: dict[uuid.UUID, float]
) -> list[LearnerKCState]:
    """This learner's current state rows for the item's KCs, in the item's own KC order."""
    states = (
        await session.scalars(
            select(LearnerKCState).where(
                LearnerKCState.learner_id == learner_id,
                LearnerKCState.kc_id.in_(list(kc_weights)),
            )
        )
    ).all()
    by_kc = {state.kc_id: state for state in states}
    return [by_kc[kc_id] for kc_id in kc_weights if kc_id in by_kc]


async def _revise_plans(
    session: AsyncSession, learner_id: uuid.UUID, kc_weights: dict[uuid.UUID, float]
) -> None:
    """Bring any plan touching these KCs back in line with the evidence just committed.

    Best-effort, and deliberately so. The grade is already durable; the plan is a projection
    that ``revise_plan`` recomputes from scratch each time. Letting a revision failure
    propagate reported a *committed* answer as failed, and the client would then retry an
    assessment it had in fact already passed. Instead the failure is logged, the plan is
    flagged, and the next read of that plan repairs it — no second assessment needed.
    """
    subject_ids: list[uuid.UUID] = []
    try:
        subject_ids = list(await knowledge_svc.subjects_for_kcs(session, kc_weights))
        for subject_id in subject_ids:
            await lesson_plan_svc.revise_plan(session, learner_id=learner_id, subject_id=subject_id)
        return
    except Exception:
        log.exception("assessment.plan_revision_failed", learner_id=str(learner_id))
    await _flag_plans_for_repair(session, learner_id, subject_ids)


async def _flag_plans_for_repair(
    session: AsyncSession, learner_id: uuid.UUID, subject_ids: list[uuid.UUID]
) -> None:
    """Leave the debt where the next plan read will find it.

    The rollback is deliberate and comes first: a revision that failed part-way may have left
    a half-applied step list on the session, and committing the flag would commit that with
    it. The cost is that ORM rows the *caller* still holds are expired — which is why
    ``answer_item`` re-reads its states after this rather than before.

    Runs on an already-failed path, so it swallows its own failures too. The worst case is a
    plan that stays stale until the learner's next answer, which still beats losing the grade
    they earned.
    """
    try:
        await session.rollback()
        for subject_id in subject_ids:
            await lesson_plan_svc.mark_revision_pending(
                session, learner_id=learner_id, subject_id=subject_id
            )
    except Exception:
        log.exception("assessment.plan_repair_flag_failed", learner_id=str(learner_id))
        await session.rollback()


async def _recorded_grade(
    session: AsyncSession, learner_id: uuid.UUID, attempt_id: uuid.UUID
) -> GradeResult | None:
    """The grade already recorded under ``attempt_id``, or ``None`` if it is a new attempt.

    Rebuilt from the event log rather than a separate results table: ``record_observation``
    writes the verdict into every row of the attempt's per-KC fan-out, so any one of them
    reconstructs the response the first request got.
    """
    events = (
        await session.scalars(
            select(LearningEvent).where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.attempt_id == attempt_id,
            )
        )
    ).all()
    if not events:
        return None
    payload = events[0].payload
    # `score` is this *row's* KC score since S10, so the item's own score comes from
    # `item_score` — falling back for events written before that key existed, where the two
    # were by definition the same number. Reading `score` here would have replayed one
    # component's mark as the whole answer's.
    item_score = payload.get("item_score", payload["score"])
    return GradeResult(
        score=item_score,
        correct=bool(payload.get("correct", item_score >= 1.0)),
        detail=payload.get("detail") or {},
        # Rebuilt from the whole fan-out rather than one row: the per-component marks are
        # exactly what is spread across it, so a replayed grade is identical to the original
        # instead of quietly losing its breakdown — or, for a grade that never had one,
        # gaining a breakdown the first response did not contain. kc_id is nullable on the
        # event table, so that filter is not decoration either.
        component_scores=(
            {e.kc_id: e.payload["score"] for e in events if e.kc_id is not None}
            if payload.get("component_scored")
            else {}
        ),
        diagnoses={
            e.kc_id: Diagnosis.model_validate(e.payload["diagnosis"])
            for e in events
            if e.kc_id is not None and e.payload.get("diagnosis")
        },
    )


async def _components_of(session: AsyncSession, item: Item) -> list[rubric_grading.GradedComponent]:
    """The KCs this item assesses, named, for the grader to mark separately (S10).

    In the item's own KC order, so component numbers are stable for a given item rather than
    dependent on however the rows came back.

    Returned for a single-KC item too, even though its score needs no breakdown: the grader
    still has to say *why* that one component fell short (S09), and the diagnosis needs a KC
    to belong to. ``grade_open`` decides from the count whether to ask for per-component
    marks.

    A rubric is attached only to the component it was actually written for: ``Rubric.kc_id``
    names one KC, so handing its criteria to every component of a multi-KC item would tell the
    grader to mark two other components against a third one's standard.
    """
    kc_ids = [link.kc_id for link in item.kc_links]
    if not kc_ids:
        return []
    rows = (await session.scalars(select(KC).where(KC.id.in_(kc_ids)))).all()
    by_id = {kc.id: kc for kc in rows}
    rubric = item.rubric
    return [
        rubric_grading.GradedComponent(
            kc_id=kc_id,
            name=by_id[kc_id].name,
            description=by_id[kc_id].description or "",
            criteria=(rubric.criteria if rubric is not None and rubric.kc_id == kc_id else None),
        )
        for kc_id in kc_ids
        if kc_id in by_id
    ]


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
            llm,
            stem=item.stem,
            response=submission.response,
            rubric=item.rubric,
            components=await _components_of(session, item),
        )
        if usage.total_tokens:  # an empty response short-circuits with no model call
            await log_llm_call(
                learner_id=learner_id,
                role=GRADING_ROLE.value,
                spec=llm.spec(GRADING_ROLE),
                usage=usage,
            )
        return result
    raise NotAutoGradable(f"{item_type} items have no grading path")
