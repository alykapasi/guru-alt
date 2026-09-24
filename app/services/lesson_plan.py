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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import lesson_plan as engine
from app.learning import mastery, prerequisites
from app.learning.placement_inference import KCCandidate
from app.llm import LLMClient
from app.models.knowledge import KC, Subject
from app.models.lesson_plan import LessonPlan
from app.schemas.lesson_plan import GoalStatusRead, LessonPlanRead
from app.services import knowledge as knowledge_svc
from app.services import profile as profile_svc
from app.services.llm_log import log_llm_call

log = structlog.get_logger(__name__)


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


async def mastered_kc_ids(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of ``kc_ids`` the learner has demonstrably mastered.

    Public because it is the system's one definition of "mastered", and a second caller
    (``app.services.checkpoints``, deciding whether a paused question is still worth asking)
    re-implementing the comparison would give the planner and the resumer the power to
    disagree about whether a learner had finished something.

    Two conditions, and the first is not redundant. The conservative estimate trades ability
    against uncertainty, so a confident-looking placement seed clears it outright; requiring
    ability evidence is what keeps a self-reported background from reading as mastery. This
    asks about *now* on purpose — retention and freshness belong to the goal's question, not
    the planner's, and a planner that waited days for a second demonstration could never
    finish a step.
    """
    ids = list(kc_ids)
    if not ids:
        return set()
    bar = get_settings().mastery_conservative_bar
    standings = await mastery.kc_standings(session, learner_id, ids)
    return {
        kc_id
        for kc_id, standing in standings.items()
        if standing.measured_at is not None and standing.current.conservative >= bar
    }


async def goal_status(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    objective_kc_ids: Sequence[str],
    closed_at: datetime | None,
    now: datetime | None = None,
) -> GoalStatusRead:
    """What the learner has demonstrated toward this goal, and how current it still is.

    Achievement deliberately ignores freshness. If it did not, a long objective could never
    be achieved: the first component learned would go stale before the last was reached, and
    the goal would sit permanently one component short with no way to catch up. Historical
    achievement and stale current evidence are two different things V0_DECISIONS asks for,
    and the learner is shown both.
    """
    now = now or datetime.now(UTC)
    kc_ids = [uuid.UUID(kc_id) for kc_id in objective_kc_ids]
    if not kc_ids:
        return GoalStatusRead(
            objective_kc_count=0,
            achieved_kc_count=0,
            current_kc_count=0,
            stale_kc_count=0,
            achieved_at=None,
            closed_at=closed_at,
        )
    settings = get_settings()
    bar = settings.mastery_conservative_bar
    max_age = timedelta(days=settings.goal_evidence_max_age_days)
    standings = await mastery.kc_standings(session, learner_id, kc_ids, now=now)

    achieved_dates = [s.achieved_at for s in standings.values() if s.achieved_at is not None]
    current = 0
    stale = 0
    for standing in standings.values():
        if standing.measured_at is None:
            continue
        if now - standing.measured_at <= max_age:
            # Decayed to now: "can they do this today".
            current += int(standing.current.conservative >= bar)
        else:
            # As of the measurement: "we saw them do this, and it was a long time ago".
            stale += int(standing.at_measurement.conservative >= bar)
    return GoalStatusRead(
        objective_kc_count=len(kc_ids),
        achieved_kc_count=len(achieved_dates),
        current_kc_count=current,
        stale_kc_count=stale,
        # The goal is achieved only when every component is, and it is dated by the last one
        # to arrive — the moment the whole objective was first true at once.
        achieved_at=max(achieved_dates) if len(achieved_dates) == len(kc_ids) else None,
        closed_at=closed_at,
    )


async def plan_read(
    session: AsyncSession, plan: LessonPlan, *, now: datetime | None = None
) -> LessonPlanRead:
    """The API's view of a plan, including a freshly computed goal status.

    Assembled here rather than in the routes so the two that return a plan cannot drift into
    reporting different things, and returned as the schema rather than folded onto the ORM
    object so nothing downstream mistakes a computed status for a stored column.
    """
    status = await goal_status(
        session,
        learner_id=plan.learner_id,
        objective_kc_ids=plan.objective_kc_ids,
        closed_at=plan.goal_closed_at,
        now=now,
    )
    return LessonPlanRead.model_validate(plan).model_copy(update={"goal_status": status})


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


def _open_detour_keys(steps: Iterable[Any]) -> set[tuple[str, str]]:
    """``(prerequisite, blocked)`` for every detour step currently open.

    Compared either side of a revision to tell a detour that was *inserted* from one that was
    merely decided: ``revise_steps`` drops a detour whose step is already open or whose KC has
    since been mastered, and a decision that changed nothing is not something the learner was
    sent on.
    """
    return {
        (str(step.get("kc_id")), str(step.get("detour_for")))
        for step in steps
        if step.get("step_type") == "detour" and step.get("status") != "done"
    }


def _apply_plan_level_hints(plan: LessonPlan, scaffolding: engine.ScaffoldingHints) -> None:
    plan.pacing = scaffolding.pacing
    plan.example_tags = scaffolding.example_tags


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
                learner_id=learner_id,
                role=engine.OBJECTIVE_ROLE.value,
                spec=llm.spec(engine.OBJECTIVE_ROLE),
                usage=usage,
            )
        if selected:
            target_ids = set(selected)

    # Validate before relying on the order (S23). A stored cycle is not hypothetical: the
    # direct prerequisite endpoint refused only self-loops until now, and any subject built
    # before that check could carry one. Dropping the closing edges here means the order that
    # follows is justified by the constraints that remain, rather than being an order
    # topo_sort invented for components it could not place.
    stored_edges = await knowledge_svc.list_edges_for_subject(session, subject_id)
    # A prerequisite living in another subject cannot be ordered inside this plan — the plan is
    # a sequence of *this* subject's components, and there is no step that could teach it. It is
    # dropped here, explicitly, rather than carried into the closure: doing the latter put a KC
    # with no tiebreak entry into the sort and raised `TypeError`, so a cross-subject edge did
    # not weaken the ordering, it stopped the plan existing (S24). What is dropped is reported
    # by `knowledge.cross_subject_prerequisites`, on the same report-don't-repair reasoning S23
    # settled on for cycles: which subject should absorb the other's component is a curriculum
    # decision the graph cannot make.
    foreign = [e for e in stored_edges if e.prereq_kc_id not in all_kc_ids]
    if foreign:
        log.warning(
            "lesson_plan.cross_subject_prerequisites_dropped",
            subject_id=str(subject_id),
            dropped=[(str(e.prereq_kc_id), str(e.kc_id)) for e in foreign],
        )
    local_edges = [e for e in stored_edges if e.prereq_kc_id in all_kc_ids]
    kept, dropped = prerequisites.acyclic([(e.prereq_kc_id, e.kc_id) for e in local_edges])
    if dropped:
        log.warning(
            "lesson_plan.cyclic_prerequisites_dropped",
            subject_id=str(subject_id),
            dropped=[(str(prereq), str(dependent)) for prereq, dependent in dropped],
        )
    edges = [engine.Edge(prereq_kc_id=prereq, kc_id=dependent) for prereq, dependent in kept]
    closure = engine.prerequisite_closure(target_ids, edges)
    # The whole objective is recorded; only its first window becomes steps. revise_plan pulls
    # the rest in as work completes, so the target — which topo-sorts last — is still reached.
    objective = engine.topo_sort(closure, edges, tiebreak)
    kc_order = objective[: get_settings().lesson_plan_max_steps]
    bare_steps = engine.build_initial_steps(kc_order)

    mastered = await mastered_kc_ids(session, learner_id, kc_order)
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
    mastered = await mastered_kc_ids(session, learner_id, new_kc_ids)
    due_reviews = await _due_review_kc_ids(session, learner_id, all_kc_ids)
    scaffolding = await _scaffolding(session, learner_id)

    # Decided against the plan *before* revision, on purpose: the answer that triggered this
    # was given for whatever step was active then, and that is the component the evidence is
    # about. Reading it after revision would ask about a step the learner has not seen.
    detour = await _prerequisite_detour(
        session, learner_id=learner_id, plan=plan, mastered=mastered
    )
    open_detours_before = _open_detour_keys(plan.steps)

    revised = engine.revise_steps(
        cast("list[engine.StepDict]", plan.steps),
        mastered_kc_ids=mastered,
        due_review_kc_ids=due_reviews,
        scaffolding=scaffolding,
        detour=detour,
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
        mastered |= await mastered_kc_ids(session, learner_id, extension_ids)
        revised = engine.revise_steps(
            [*revised, *engine.build_initial_steps(extension_ids)],
            mastered_kc_ids=mastered,
            due_review_kc_ids=due_reviews,
            scaffolding=scaffolding,
            # Not passed again: the first pass already inserted it, and re-deciding here would
            # append a second identical step for the same prerequisite.
        )
    # Recorded on *insertion*, not on decision: ``revise_steps`` declines a detour whose step
    # is already open or whose KC turned out to be mastered, and counting a decision that
    # changed nothing would make the cap fire on detours the learner was never sent on.
    if detour is not None:
        inserted = _open_detour_keys(revised) - open_detours_before
        if (str(detour.prereq_kc_id), str(detour.blocked_kc_id)) in inserted:
            mastery.record_detour(
                session,
                learner_id=learner_id,
                blocked_kc_id=detour.blocked_kc_id,
                prereq_kc_id=detour.prereq_kc_id,
                reason=detour.reason,
                consecutive_failures=detour.consecutive_failures,
            )

    plan.steps = cast("list[dict[str, Any]]", revised)
    _apply_plan_level_hints(plan, scaffolding)
    plan.revision_pending = False  # whatever was owed, this recomputation covers it

    await session.commit()
    await session.refresh(plan)
    return plan


async def _prerequisite_detour(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    plan: LessonPlan,
    mastered: set[uuid.UUID],
) -> engine.Detour | None:
    """Whether this learner should be sent to a prerequisite before the step they are on (S11).

    Ordered so the cheap checks come first. Most revisions run after an answer that went fine,
    and those must not pay for the graph queries: the struggle read is one indexed query on
    events the learner already produced, and everything else is skipped unless it finds
    something.
    """
    active = next(
        (
            step
            for step in plan.steps
            if step.get("status") == "active" and step.get("step_type") == "new"
        ),
        None,
    )
    if active is None:
        return None  # a review or an existing detour is active; nothing is blocked here
    blocked_id = uuid.UUID(active["kc_id"])
    if blocked_id in mastered:
        return None  # they just finished it — the failure that prompted this is history

    settings = get_settings()
    struggle = await mastery.recent_struggle(
        session, learner_id, blocked_id, threshold=settings.detour_failure_threshold
    )
    if (
        not struggle.diagnosed_prerequisite
        and struggle.consecutive_failures < settings.detour_min_failures
    ):
        return None

    edges = await knowledge_svc.list_prerequisites(session, blocked_id)
    prereq_ids = [edge.prereq_kc_id for edge in edges]
    if not prereq_ids:
        return None  # nothing upstream to detour to, so the difficulty is here

    # Drop the ones already tried to exhaustion for this component. The trigger reads the
    # *current* run of failures, which resets nothing about history: a learner stuck on a
    # component whose prerequisite is not the real problem was detoured to it again after
    # every failed attempt. Applied to the candidates rather than to the decision so that a
    # graded prerequisite name pointing at an exhausted component falls through to the next
    # plausible one instead of suppressing the detour entirely.
    tried = await mastery.detour_attempts(session, learner_id, blocked_id)
    prereq_ids = [
        kc_id for kc_id in prereq_ids if tried.get(kc_id, 0) < settings.detour_max_repeats
    ]
    if not prereq_ids:
        return None  # every route upstream has been tried; the difficulty is not up there

    names = {kc.id: kc.name for kc in await knowledge_svc.get_kcs(session, prereq_ids)}
    return engine.prerequisite_detour(
        blocked_kc_id=blocked_id,
        prerequisites=prereq_ids,
        prerequisite_names=names,
        mastered=await mastered_kc_ids(session, learner_id, prereq_ids),
        diagnosed_name=struggle.diagnosed_prerequisite,
        consecutive_failures=struggle.consecutive_failures,
        min_failures=settings.detour_min_failures,
    )


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
