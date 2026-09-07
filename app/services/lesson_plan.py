"""The lesson plan: DB-facing orchestration around the pure policy layer.

Mirrors ``mastery.py``/``profile.py``'s split from their pure/LLM logic — the graph
algorithms and step-revision policy live in ``app.learning.lesson_plan``; this module owns
the I/O (candidate KCs, mastery, profile, persistence).

Two entry points with very different cost: ``generate_lesson_plan`` is the expensive,
on-demand path (an LLM call for objective selection, plus prerequisite-closure/topo-sort);
``revise_plan`` is the cheap, auto-triggered path (no LLM, no topo-sort recompute) that keeps
an *existing* plan's status/order/hints current as evidence arrives (graded answers, due
reviews, profile shifts) — see ``app.learning.lesson_plan.revise_steps``.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import lesson_plan as engine
from app.learning import mastery
from app.learning.placement_inference import KCCandidate
from app.llm import LLMClient
from app.models.knowledge import KC, Subject
from app.models.lesson_plan import LessonPlan
from app.services import knowledge as knowledge_svc
from app.services import profile as profile_svc
from app.services.llm_log import log_llm_call

log = structlog.get_logger(__name__)

MASTERY_ABILITY_THRESHOLD = 1.0
MASTERY_UNCERTAINTY_THRESHOLD = 0.5
"""v1-arbitrary "well mastered" bar — same spirit as placement's level->estimate mapping,
not calibrated against real outcome data."""


@dataclass(frozen=True)
class PlanGroundingContext:
    """The learner's current active step, shaped for folding into a tutor-turn system prompt
    (and, via ``kc_id``, for the session runner to resolve a practice item)."""

    subject_name: str
    kc_id: uuid.UUID
    kc_name: str
    step_type: engine.StepType
    target_difficulty: float | None
    hint_density: str | None
    preferred_item_type: str | None


async def _get_plan(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> LessonPlan | None:
    return await session.scalar(
        select(LessonPlan).where(
            LessonPlan.learner_id == learner_id, LessonPlan.subject_id == subject_id
        )
    )


async def _mastered_kc_ids(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    mastered: set[uuid.UUID] = set()
    for kc_id in kc_ids:
        estimate = await mastery.estimate_kc(session, learner_id, kc_id)
        if (
            estimate.ability >= MASTERY_ABILITY_THRESHOLD
            and estimate.uncertainty <= MASTERY_UNCERTAINTY_THRESHOLD
        ):
            mastered.add(kc_id)
    return mastered


async def _due_review_kc_ids(
    session: AsyncSession, learner_id: uuid.UUID, subject_kc_ids: set[uuid.UUID]
) -> list[uuid.UUID]:
    """Soonest-due first, filtered to this subject's KCs (``mastery.due_reviews`` is global)."""
    reviews = await mastery.due_reviews(session, learner_id)
    return [r.kc_id for r in reviews if r.kc_id in subject_kc_ids]


async def _scaffolding(session: AsyncSession, learner_id: uuid.UUID) -> engine.ScaffoldingHints:
    snapshot = await profile_svc.get_snapshot(session, learner_id)
    values = {d.key: d.value for d in snapshot}
    return engine.scaffolding_from_profile(values)


def _apply_plan_level_hints(plan: LessonPlan, scaffolding: engine.ScaffoldingHints) -> None:
    plan.pacing = scaffolding.pacing
    plan.example_tags = scaffolding.example_tags
    plan.reading_level_hint = scaffolding.reading_level_hint


async def generate_lesson_plan(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID,
    goal: str | None,
) -> LessonPlan:
    """Regenerate a learner's plan for a subject from scratch (query-or-create; replaces
    ``goal``/``steps``/the plan-level hints in place — no versioning)."""
    kcs = list(await knowledge_svc.list_kcs_for_subject(session, subject_id))
    # list_kcs_for_subject already orders by (topic.slug, kc.slug); reuse that order as the
    # topo-sort tiebreak rather than re-deriving it (and re-touching kc.topic, unloaded here).
    tiebreak = {kc.id: i for i, kc in enumerate(kcs)}
    all_kc_ids = set(tiebreak)

    target_ids = all_kc_ids
    if goal and kcs:
        candidates = [KCCandidate(id=kc.id, name=kc.name, description=kc.description) for kc in kcs]
        selected, usage = await engine.select_objectives(llm, goal, candidates)
        if usage.total_tokens:
            await log_llm_call(
                session,
                learner_id=learner_id,
                role=engine.OBJECTIVE_ROLE.value,
                spec=llm.spec(engine.OBJECTIVE_ROLE),
                usage=usage,
            )
        if selected:
            target_ids = set(selected)

    edges = [
        engine.Edge(prereq_kc_id=e.prereq_kc_id, kc_id=e.kc_id)
        for e in await knowledge_svc.list_edges_for_subject(session, subject_id)
    ]
    closure = engine.prerequisite_closure(target_ids, edges)
    # The whole objective is recorded; only its first window becomes steps. revise_plan pulls
    # the rest in as work completes, so the target — which topo-sorts last — is still reached.
    objective = engine.topo_sort(closure, edges, tiebreak)
    kc_order = objective[: get_settings().lesson_plan_max_steps]
    bare_steps = engine.build_initial_steps(kc_order)

    mastered = await _mastered_kc_ids(session, learner_id, kc_order)
    due_reviews = await _due_review_kc_ids(session, learner_id, all_kc_ids)
    scaffolding = await _scaffolding(session, learner_id)
    steps = engine.revise_steps(
        bare_steps,
        mastered_kc_ids=mastered,
        due_review_kc_ids=due_reviews,
        scaffolding=scaffolding,
    )

    plan = await _get_plan(session, learner_id, subject_id)
    if plan is None:
        plan = LessonPlan(learner_id=learner_id, subject_id=subject_id)
        session.add(plan)
    plan.goal = goal
    plan.objective_kc_ids = [str(kc_id) for kc_id in objective]
    plan.steps = cast("list[dict[str, Any]]", steps)
    _apply_plan_level_hints(plan, scaffolding)

    await session.commit()
    await session.refresh(plan)
    return plan


async def revise_plan(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> LessonPlan | None:
    """Re-derive an *existing* plan's status/order/hints from current mastery/due-reviews/
    profile — no LLM call, no topo-sort recompute. No-ops (returns ``None``) if the learner
    has no plan for this subject yet; evidence on a subject nobody has planned shouldn't
    create one implicitly.

    Also advances the horizon: components of the objective that did not fit the step cap move
    in as earlier ones finish, so a goal bigger than the cap is worked through rather than
    truncated (:func:`engine.horizon_extension`).
    """
    plan = await _get_plan(session, learner_id, subject_id)
    if plan is None:
        return None

    new_kc_ids = {uuid.UUID(step["kc_id"]) for step in plan.steps if step["step_type"] == "new"}
    all_kc_ids = {kc.id for kc in await knowledge_svc.list_kcs_for_subject(session, subject_id)}
    mastered = await _mastered_kc_ids(session, learner_id, new_kc_ids)
    due_reviews = await _due_review_kc_ids(session, learner_id, all_kc_ids)
    scaffolding = await _scaffolding(session, learner_id)

    revised = engine.revise_steps(
        cast("list[engine.StepDict]", plan.steps),
        mastered_kc_ids=mastered,
        due_review_kc_ids=due_reviews,
        scaffolding=scaffolding,
    )
    # Only now is it known which steps this revision finished, and so how much room the
    # horizon has for the rest of the objective. Extending before that would never see any.
    extension = engine.horizon_extension(
        plan.objective_kc_ids, revised, max_open_steps=get_settings().lesson_plan_max_steps
    )
    if extension:
        extension_ids = [uuid.UUID(kc_id) for kc_id in extension]
        # A KC arriving from the deferred tail may already be mastered (placement, or work in
        # another plan), so it gets the same status derivation as anything else.
        mastered |= await _mastered_kc_ids(session, learner_id, extension_ids)
        revised = engine.revise_steps(
            [*revised, *engine.build_initial_steps(extension_ids)],
            mastered_kc_ids=mastered,
            due_review_kc_ids=due_reviews,
            scaffolding=scaffolding,
        )
    plan.steps = cast("list[dict[str, Any]]", revised)
    _apply_plan_level_hints(plan, scaffolding)
    plan.revision_pending = False  # whatever was owed, this recomputation covers it

    await session.commit()
    await session.refresh(plan)
    return plan


async def mark_revision_pending(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> None:
    """Record that this plan is behind the evidence, so the next read brings it up to date.

    Set only when a revision failed *after* its triggering answer had already committed: the
    grade is authoritative and gets reported, and the derived plan carries the debt instead of
    the learner being told their answer failed. See ``services.assessment.answer_item``.
    """
    await session.execute(
        update(LessonPlan)
        .where(LessonPlan.learner_id == learner_id, LessonPlan.subject_id == subject_id)
        .values(revision_pending=True)
    )
    await session.commit()


async def get_lesson_plan(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> LessonPlan | None:
    """Read-only — no recompute, unless the plan is carrying an unpaid revision.

    ``revision_pending`` means a revision this plan was owed failed after its triggering
    answer had committed, so the plan is behind evidence the learner has already produced.
    Repairing it here is what lets it catch up without the learner having to answer anything
    else — and, like the revision that failed, it is best-effort: a stale plan is worth
    showing, a 500 is not.
    """
    plan = await _get_plan(session, learner_id, subject_id)
    if plan is None or not plan.revision_pending:
        return plan
    try:
        return await revise_plan(session, learner_id=learner_id, subject_id=subject_id) or plan
    except Exception:
        log.exception(
            "lesson_plan.pending_revision_repair_failed",
            learner_id=str(learner_id),
            subject_id=str(subject_id),
        )
        await session.rollback()
        return plan


async def get_active_step_context(
    session: AsyncSession, learner_id: uuid.UUID, *, subject_id: uuid.UUID | None = None
) -> PlanGroundingContext | None:
    """The active step of the learner's plan for ``subject_id``, for tutor-turn grounding and
    the session runner's practice item.

    If ``subject_id`` is given, this is an exact lookup — ``None`` if that subject has no plan
    yet (never falls back to a different subject's plan). If omitted (subject-less
    conversations), falls back to the learner's most-recently-updated plan across all
    subjects — the v1 heuristic from before conversations were subject-scoped, kept for
    conversations that still aren't.
    """
    if subject_id is not None:
        plan = await _get_plan(session, learner_id, subject_id)
    else:
        plan = await session.scalar(
            select(LessonPlan)
            .where(LessonPlan.learner_id == learner_id)
            .order_by(LessonPlan.updated_at.desc())
            .limit(1)
        )
    if plan is None:
        return None
    active = next((s for s in plan.steps if s["status"] == "active"), None)
    if active is None:
        return None
    kc_id = uuid.UUID(active["kc_id"])
    kc = await session.get(KC, kc_id)
    subject = await session.get(Subject, plan.subject_id)
    if kc is None or subject is None:
        return None
    return PlanGroundingContext(
        subject_name=subject.name,
        kc_id=kc_id,
        kc_name=kc.name,
        step_type=active["step_type"],
        target_difficulty=active["target_difficulty"],
        hint_density=active["hint_density"],
        preferred_item_type=active["preferred_item_type"],
    )
