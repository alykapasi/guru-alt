"""When a prerequisite is what is actually blocking them, go there and come back (S11).

Revision reordered steps, flipped statuses and refreshed scaffolding hints — all of it within
the order the plan was generated with. Nothing ever said "the reason you cannot do this is
something earlier", and nothing sent the learner there. A learner stuck on least squares
because they never learned projections got the same component again, rescaffolded.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import lesson_plan as engine
from app.learning import mastery
from app.learning.diagnosis import FailureKind
from app.learning.lesson_plan import (
    DETOUR_DIAGNOSED,
    DETOUR_REPEATED_FAILURE,
    Detour,
    DetourNotOpen,
    ScaffoldingHints,
    StepDict,
    StepStatus,
    StepType,
    decide_detour,
    prerequisite_detour,
    revise_steps,
)
from app.learning.mastery import Observation
from app.llm.registry import fake_llm_client
from app.models.assessment import RUBRIC_GRADABLE, EvidenceKind
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.profile import ProfileDimension
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as svc

NO_HINTS = ScaffoldingHints()


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# --- the decision ------------------------------------------------------------


def test_a_named_prerequisite_is_acted_on_immediately() -> None:
    """Being told the failure is upstream is the entire point of asking (S09), so it does not
    wait for a second failure."""
    blocked, prereq = _ids(2)
    detour = prerequisite_detour(
        blocked_kc_id=blocked,
        prerequisites=[prereq],
        prerequisite_names={prereq: "Orthogonal projection"},
        mastered=[],
        diagnosed_name="orthogonal  PROJECTION",
        consecutive_failures=1,
    )
    assert detour == Detour(
        prereq_kc_id=prereq,
        blocked_kc_id=blocked,
        reason=DETOUR_DIAGNOSED,
        consecutive_failures=1,
    )


def test_repeated_failure_alone_is_enough() -> None:
    # Most generated items are MCQs, which produce no diagnosis at all. Without this trigger
    # detours would only ever fire on open questions.
    blocked, first, second = _ids(3)
    detour = prerequisite_detour(
        blocked_kc_id=blocked,
        prerequisites=[first, second],
        prerequisite_names={first: "A", second: "B"},
        mastered=[],
        consecutive_failures=2,
        min_failures=2,
    )
    assert detour is not None
    assert detour.prereq_kc_id == first  # nearest in prerequisite order
    assert detour.reason == DETOUR_REPEATED_FAILURE


def test_one_bad_answer_is_not_a_blocked_learner() -> None:
    blocked, prereq = _ids(2)
    assert (
        prerequisite_detour(
            blocked_kc_id=blocked,
            prerequisites=[prereq],
            prerequisite_names={prereq: "A"},
            mastered=[],
            consecutive_failures=1,
            min_failures=2,
        )
        is None
    )


def test_a_mastered_prerequisite_is_not_where_the_problem_is() -> None:
    blocked, done, outstanding = _ids(3)
    detour = prerequisite_detour(
        blocked_kc_id=blocked,
        prerequisites=[done, outstanding],
        prerequisite_names={done: "Done", outstanding: "Outstanding"},
        mastered=[done],
        consecutive_failures=3,
    )
    assert detour is not None and detour.prereq_kc_id == outstanding


def test_with_everything_upstream_mastered_the_difficulty_is_here() -> None:
    blocked, prereq = _ids(2)
    assert (
        prerequisite_detour(
            blocked_kc_id=blocked,
            prerequisites=[prereq],
            prerequisite_names={prereq: "A"},
            mastered=[prereq],
            diagnosed_name="A",
            consecutive_failures=9,
        )
        is None
    )


def test_a_name_matching_nothing_in_the_graph_is_dropped() -> None:
    """The grader sees the question, not the graph, so it can name something true and
    irrelevant — or something that is not a component at all."""
    blocked, prereq = _ids(2)
    assert (
        prerequisite_detour(
            blocked_kc_id=blocked,
            prerequisites=[prereq],
            prerequisite_names={prereq: "Projections"},
            mastered=[],
            diagnosed_name="undergraduate willpower",
            consecutive_failures=1,
            min_failures=2,
        )
        is None
    )


def test_a_name_that_matches_nothing_still_leaves_the_failure_trigger() -> None:
    blocked, prereq = _ids(2)
    detour = prerequisite_detour(
        blocked_kc_id=blocked,
        prerequisites=[prereq],
        prerequisite_names={prereq: "Projections"},
        mastered=[],
        diagnosed_name="nonsense",
        consecutive_failures=2,
        min_failures=2,
    )
    assert detour is not None and detour.reason == DETOUR_REPEATED_FAILURE


def test_no_prerequisites_means_no_detour() -> None:
    assert (
        prerequisite_detour(
            blocked_kc_id=uuid.uuid4(),
            prerequisites=[],
            prerequisite_names={},
            mastered=[],
            consecutive_failures=5,
        )
        is None
    )


# --- the plan ----------------------------------------------------------------


def _step(
    kc_id: uuid.UUID, step_type: StepType = "new", status: StepStatus = "pending"
) -> StepDict:
    return StepDict(
        kc_id=str(kc_id),
        order=0,
        step_type=step_type,
        status=status,
        target_difficulty=None,
        hint_density=None,
        preferred_item_type=None,
    )


def test_a_detour_step_goes_in_front_of_the_step_it_unblocks() -> None:
    blocked, prereq = _ids(2)
    revised = revise_steps(
        [_step(blocked, status="active")],
        mastered_kc_ids=[],
        due_review_kc_ids=[],
        scaffolding=NO_HINTS,
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
    )
    assert [s["kc_id"] for s in revised] == [str(prereq), str(blocked)]
    assert revised[0]["step_type"] == "detour"
    assert revised[0]["status"] == "active"
    assert revised[0]["detour_for"] == str(blocked)
    assert revised[1]["status"] == "pending"


def test_a_detour_comes_before_due_reviews() -> None:
    """A detour is the direct response to the failure that just happened; a queue of
    flashcards between the two breaks that connection, and FSRS intervals are days long."""
    blocked, prereq, due = _ids(3)
    revised = revise_steps(
        [_step(blocked, status="active")],
        mastered_kc_ids=[],
        due_review_kc_ids=[due],
        scaffolding=NO_HINTS,
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
    )
    assert revised[0]["kc_id"] == str(prereq)
    assert revised[1]["kc_id"] == str(due)


def test_finishing_the_detour_returns_to_the_original_objective() -> None:
    # No separate "return" mechanism: the detour retires like any other step and the one it
    # was blocking becomes active again.
    blocked, prereq = _ids(2)
    with_detour = revise_steps(
        [_step(blocked, status="active")],
        mastered_kc_ids=[],
        due_review_kc_ids=[],
        scaffolding=NO_HINTS,
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
    )
    after = revise_steps(
        with_detour, mastered_kc_ids=[prereq], due_review_kc_ids=[], scaffolding=NO_HINTS
    )
    done = next(s for s in after if s["step_type"] == "detour")
    assert done["status"] == "done"
    assert next(s for s in after if s["step_type"] == "new")["status"] == "active"


def test_the_same_detour_is_not_added_twice() -> None:
    blocked, prereq = _ids(2)
    detour = Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED)
    once = revise_steps(
        [_step(blocked, status="active")],
        mastered_kc_ids=[],
        due_review_kc_ids=[],
        scaffolding=NO_HINTS,
        detour=detour,
    )
    twice = revise_steps(
        once, mastered_kc_ids=[], due_review_kc_ids=[], scaffolding=NO_HINTS, detour=detour
    )
    assert sum(1 for s in twice if s["step_type"] == "detour") == 1


def test_a_detour_to_something_already_mastered_is_not_inserted() -> None:
    blocked, prereq = _ids(2)
    revised = revise_steps(
        [_step(blocked, status="active")],
        mastered_kc_ids=[prereq],
        due_review_kc_ids=[],
        scaffolding=NO_HINTS,
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
    )
    assert not any(s["step_type"] == "detour" for s in revised)


def test_plans_written_before_detours_existed_revise_unchanged() -> None:
    a, b = _ids(2)
    revised = revise_steps(
        [_step(a, status="active"), _step(b)],
        mastered_kc_ids=[],
        due_review_kc_ids=[],
        scaffolding=NO_HINTS,
    )
    assert [s["kc_id"] for s in revised] == [str(a), str(b)]
    assert revised[0]["status"] == "active"


# --- the learner's say (S11, V07) --------------------------------------------

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _with_detour(guidance: str = "guided") -> tuple[list[StepDict], uuid.UUID, uuid.UUID]:
    blocked, prereq = _ids(2)
    steps = revise_steps(
        [_step(blocked, status="active")],
        mastered_kc_ids=[],
        due_review_kc_ids=[],
        scaffolding=NO_HINTS,
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
        guidance=guidance,  # ty: ignore[invalid-argument-type]
        now=NOW,
    )
    return steps, blocked, prereq


def _by_kc(steps: list[StepDict], kc: uuid.UUID) -> StepDict:
    return next(s for s in steps if s["kc_id"] == str(kc))


def _revise(steps: list[StepDict], **kwargs: Any) -> list[StepDict]:
    base: dict[str, Any] = {"mastered_kc_ids": [], "due_review_kc_ids": [], "scaffolding": NO_HINTS}
    return revise_steps(steps, **(base | kwargs))


def test_guided_takes_the_detour_and_records_when_it_opened() -> None:
    steps, blocked, prereq = _with_detour("guided")
    assert _by_kc(steps, prereq)["status"] == "active"
    assert _by_kc(steps, prereq)["opened_at"] == NOW.isoformat()
    assert _by_kc(steps, blocked)["status"] == "pending"


def test_exploration_only_proposes() -> None:
    steps, blocked, prereq = _with_detour("exploration")
    proposal = _by_kc(steps, prereq)
    assert proposal["status"] == "proposed"
    assert proposal.get("opened_at") is None
    assert _by_kc(steps, blocked)["status"] == "active"
    # Directly before the step it was proposed for.
    assert [s["kc_id"] for s in steps] == [str(prereq), str(blocked)]


def test_offered_at_is_stamped_on_insertion_in_both_modes() -> None:
    """Fix round 2 (S11): unlike ``opened_at``, ``offered_at`` marks when a step first
    entered the plan regardless of guidance — it is what a caller keys a per-step outcome on,
    since a route can legitimately reopen after closing ``mastered`` and the route alone
    cannot tell two steps on it apart."""
    for guidance in ("guided", "exploration"):
        steps, _blocked, prereq = _with_detour(guidance)
        assert _by_kc(steps, prereq)["offered_at"] == NOW.isoformat()


def test_a_proposal_is_never_activated_by_revision() -> None:
    steps, blocked, prereq = _with_detour("exploration")
    for _ in range(3):
        steps = _revise(steps, guidance="exploration")
    assert _by_kc(steps, prereq)["status"] == "proposed"
    assert _by_kc(steps, blocked)["status"] == "active"


def test_a_proposal_blocks_a_second_one_for_the_same_prerequisite() -> None:
    steps, blocked, prereq = _with_detour("exploration")
    again = _revise(
        steps,
        guidance="exploration",
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
    )
    assert sum(1 for s in again if s["step_type"] == "detour") == 1


def test_accepting_makes_it_the_active_step() -> None:
    steps, blocked, prereq = _with_detour("exploration")
    later = NOW + timedelta(minutes=5)
    steps = _revise(
        decide_detour(steps, prereq_kc_id=prereq, decision="accept", now=later),
        guidance="exploration",
    )
    assert _by_kc(steps, prereq)["status"] == "active"
    assert _by_kc(steps, prereq)["opened_at"] == later.isoformat()
    assert _by_kc(steps, blocked)["status"] == "pending"


def test_skipping_returns_to_the_blocked_step_in_either_mode() -> None:
    for guidance in ("guided", "exploration"):
        steps, blocked, prereq = _with_detour(guidance)
        steps = _revise(
            decide_detour(steps, prereq_kc_id=prereq, decision="skip", now=NOW),
            guidance=guidance,
        )
        skipped = _by_kc(steps, prereq)
        assert skipped["status"] == "skipped"
        assert skipped["detour_outcome"] == "skipped"
        assert _by_kc(steps, blocked)["status"] == "active"


def test_a_skipped_detour_does_not_stop_a_new_one_being_offered() -> None:
    # "Already open" must not count a skipped step; whether that route is *allowed* again is
    # the service's call (closed_detour_routes), not the engine's.
    steps, blocked, prereq = _with_detour("guided")
    steps = decide_detour(steps, prereq_kc_id=prereq, decision="skip", now=NOW)
    again = _revise(
        steps,
        detour=Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=DETOUR_DIAGNOSED),
        now=NOW,
    )
    assert [s["status"] for s in again if s["step_type"] == "detour"].count("active") == 1


def test_accept_on_an_accepted_detour_is_refused() -> None:
    steps, _blocked, prereq = _with_detour("guided")
    with pytest.raises(DetourNotOpen):
        decide_detour(steps, prereq_kc_id=prereq, decision="accept", now=NOW)


def test_deciding_on_a_detour_that_is_not_there_is_refused() -> None:
    steps, _blocked, _prereq = _with_detour("guided")
    with pytest.raises(DetourNotOpen):
        decide_detour(steps, prereq_kc_id=uuid.uuid4(), decision="skip", now=NOW)


def test_a_disproved_gap_ends_the_detour_and_goes_back() -> None:
    steps, blocked, prereq = _with_detour("guided")
    steps = _revise(steps, disproved_kc_ids=[prereq])
    done = _by_kc(steps, prereq)
    assert (done["status"], done["detour_outcome"]) == ("done", "disproved")
    assert _by_kc(steps, blocked)["status"] == "active"


def test_mastery_wins_over_disproval() -> None:
    steps, _blocked, prereq = _with_detour("guided")
    steps = _revise(steps, mastered_kc_ids=[prereq], disproved_kc_ids=[prereq])
    assert _by_kc(steps, prereq)["detour_outcome"] == "mastered"


def test_a_detour_with_no_start_time_cannot_be_disproved() -> None:
    # Written before this slice: no trustworthy start, so an old pass must not close it.
    steps, _blocked, prereq = _with_detour("guided")
    _by_kc(steps, prereq).pop("opened_at")
    steps = _revise(steps, disproved_kc_ids=[prereq])
    assert _by_kc(steps, prereq)["status"] == "active"


def test_a_proposal_is_not_disproved_but_is_dropped_once_mastered() -> None:
    steps, _blocked, prereq = _with_detour("exploration")
    assert _by_kc(_revise(steps, disproved_kc_ids=[prereq]), prereq)["status"] == "proposed"
    dropped = _by_kc(_revise(steps, mastered_kc_ids=[prereq]), prereq)
    assert (dropped["status"], dropped["detour_outcome"]) == ("done", "mastered")


def test_a_hand_built_proposal_with_an_opened_at_stamp_is_still_never_disproved() -> None:
    # `revise_steps` itself never puts an `opened_at` on a proposal, but a step dict handed
    # back in (e.g. round-tripped from persistence, or built by another caller) should not
    # depend on that invariant holding elsewhere — the "proposed" status alone, not the
    # absence of a timestamp, is what protects an offer from being treated as disproved.
    blocked, prereq = _ids(2)
    hand_built = StepDict(
        kc_id=str(prereq),
        order=0,
        step_type="detour",
        status="proposed",
        target_difficulty=None,
        hint_density=None,
        preferred_item_type=None,
        detour_for=str(blocked),
        detour_reason=DETOUR_DIAGNOSED,
        opened_at=NOW.isoformat(),
    )
    steps = _revise([hand_built, _step(blocked, status="active")], disproved_kc_ids=[prereq])
    proposal = _by_kc(steps, prereq)
    assert proposal["status"] == "proposed"
    assert proposal.get("detour_outcome") is None


def test_a_proposal_is_dropped_once_its_step_is_done() -> None:
    # An offer nobody answered is not a decision left open — once the step it was for is done
    # some other way, the offer is stale and revision drops it rather than leaving it stranded.
    steps, blocked, prereq = _with_detour("exploration")
    after = _revise(steps, mastered_kc_ids=[blocked])
    assert not any(s["kc_id"] == str(prereq) for s in after)


def test_a_proposal_is_dropped_when_its_step_is_no_longer_in_the_plan() -> None:
    steps, blocked, prereq = _with_detour("exploration")
    without_blocked = [s for s in steps if s["kc_id"] != str(blocked)]
    after = _revise(without_blocked)
    assert not any(s["kc_id"] == str(prereq) for s in after)


def test_a_guided_detour_is_not_dropped_by_the_new_pruning_rule() -> None:
    # `pending`/`active` detours are a decision already acted on, not an outstanding offer —
    # the pruning rule only ever touches proposals, so guided mode is unchanged.
    steps, blocked, prereq = _with_detour("guided")
    without_blocked = [s for s in steps if s["kc_id"] != str(blocked)]
    after = _revise(without_blocked)
    assert _by_kc(after, prereq)["status"] == "active"


# --- the struggle signal -----------------------------------------------------


async def _graph(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    learner = Learner(handle=f"p-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Linear Algebra")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    prereq = KC(topic_id=topic.id, slug="a-projection", name="Orthogonal projection")
    blocked = KC(topic_id=topic.id, slug="b-least-squares", name="Least squares")
    session.add_all([prereq, blocked])
    await session.flush()
    session.add(KCEdge(prereq_kc_id=prereq.id, kc_id=blocked.id))
    await session.flush()
    return learner, subject, prereq, blocked


async def _observe(
    session: AsyncSession, learner: Learner, kc: KC, score: float, *, diagnosis: dict | None = None
) -> None:
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=score,
            kc_diagnoses={kc.id: diagnosis} if diagnosis else None,
        ),
    )
    await session.flush()


async def test_a_run_of_failures_is_counted_back_from_the_latest(
    db_session: AsyncSession,
) -> None:
    learner, _, _, blocked = await _graph(db_session)
    await _observe(db_session, learner, blocked, 0.1)
    await _observe(db_session, learner, blocked, 0.2)

    struggle = await mastery.recent_struggle(db_session, learner.id, blocked.id, threshold=0.5)
    assert struggle.consecutive_failures == 2


async def test_a_success_ends_the_run(db_session: AsyncSession) -> None:
    """A learner who failed twice and then succeeded is not stuck, and a lifetime tally would
    say they were forever."""
    learner, _, _, blocked = await _graph(db_session)
    await _observe(db_session, learner, blocked, 0.1)
    await _observe(db_session, learner, blocked, 0.2)
    await _observe(db_session, learner, blocked, 1.0)

    struggle = await mastery.recent_struggle(db_session, learner.id, blocked.id, threshold=0.5)
    assert struggle.consecutive_failures == 0


async def test_the_named_prerequisite_is_read_off_the_event_log(
    db_session: AsyncSession,
) -> None:
    learner, _, _, blocked = await _graph(db_session)
    await _observe(
        db_session,
        learner,
        blocked,
        0.2,
        diagnosis={
            "kind": FailureKind.PREREQUISITE.value,
            "prerequisite": "Orthogonal projection",
            "confidence": 0.8,
            "evidence": "",
            "evidence_verbatim": False,
        },
    )
    struggle = await mastery.recent_struggle(db_session, learner.id, blocked.id, threshold=0.5)
    assert struggle.diagnosed_prerequisite == "Orthogonal projection"


# --- end to end --------------------------------------------------------------


async def _plan(session: AsyncSession, learner: Learner, subject: Subject):
    return await svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )


async def test_a_stuck_learner_is_sent_to_the_prerequisite(db_session: AsyncSession) -> None:
    learner, subject, prereq, blocked = await _graph(db_session)
    # Mastered the prerequisite's *step* out of the way so least squares is what is active.
    # Mastery takes ability evidence as well as a confident row, so the fixture has to have
    # been measured — an unmeasured row is a placement seed, and the step would stay active.
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=prereq.id,
            ability=2.0,
            uncertainty=0.2,
            last_seen_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    plan = await _plan(db_session, learner, subject)
    assert plan is not None
    active = next(s for s in plan.steps if s["status"] == "active")
    assert active["kc_id"] == str(blocked.id)

    # Now let the prerequisite lapse, and fail least squares twice.
    state = await db_session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == prereq.id
        )
    )
    assert state is not None
    state.ability = -1.0
    state.uncertainty = 0.9
    await db_session.flush()
    await _observe(db_session, learner, blocked, 0.1)
    await _observe(db_session, learner, blocked, 0.1)

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    first = revised.steps[0]
    assert first["step_type"] == "detour"
    assert first["kc_id"] == str(prereq.id)
    assert first["detour_for"] == str(blocked.id)
    assert first["status"] == "active"


async def test_a_learner_doing_fine_is_not_detoured(db_session: AsyncSession) -> None:
    learner, subject, _prereq, blocked = await _graph(db_session)
    plan = await _plan(db_session, learner, subject)
    assert plan is not None
    await _observe(db_session, learner, blocked, 1.0)

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    assert not any(s["step_type"] == "detour" for s in revised.steps)


async def test_which_prerequisite_a_stuck_learner_is_sent_to_is_decided_not_observed(
    db_session: AsyncSession,
) -> None:
    """The behavioural trigger picks the *first* outstanding prerequisite, so an unordered
    scan would let the query planner choose where a stuck learner goes — and choose
    differently on a later revision. The same defect S23 found in subject-wide edges, in a
    place where the consequence is what the learner is taught next.

    Rows are inserted in the opposite order to their timestamps, so heap order and declaration
    order genuinely disagree.
    """
    learner = Learner(handle=f"p-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    first = KC(topic_id=topic.id, slug="a-first", name="First")
    second = KC(topic_id=topic.id, slug="b-second", name="Second")
    blocked = KC(topic_id=topic.id, slug="c-blocked", name="Blocked")
    db_session.add_all([first, second, blocked])
    await db_session.flush()
    db_session.add(
        KCEdge(prereq_kc_id=second.id, kc_id=blocked.id, created_at=datetime(2026, 6, 1, 10, 0))
    )
    await db_session.flush()
    db_session.add(
        KCEdge(prereq_kc_id=first.id, kc_id=blocked.id, created_at=datetime(2026, 6, 1, 9, 0))
    )
    await db_session.flush()

    edges = await knowledge_svc.list_prerequisites(db_session, blocked.id)
    assert [e.prereq_kc_id for e in edges] == [first.id, second.id]

    detour = prerequisite_detour(
        blocked_kc_id=blocked.id,
        prerequisites=[e.prereq_kc_id for e in edges],
        prerequisite_names={first.id: "First", second.id: "Second"},
        mastered=[],
        consecutive_failures=2,
    )
    assert detour is not None and detour.prereq_kc_id == first.id


# --- recorded, capped, and asked in a format that can answer the question (S11) ------------


async def _stuck(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    """A learner active on ``blocked`` with a lapsed prerequisite and two failures behind them."""
    learner, subject, prereq, blocked = await _graph(session)
    # Measured, not merely asserted: mastery needs ability evidence, and this row exists to
    # carry the prerequisite's step past the planner before it is lapsed below.
    session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=prereq.id,
            ability=2.0,
            uncertainty=0.2,
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await _plan(session, learner, subject)
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == prereq.id
        )
    )
    assert state is not None
    state.ability = -1.0
    state.uncertainty = 0.9
    await session.flush()
    await _observe(session, learner, blocked, 0.1)
    await _observe(session, learner, blocked, 0.1)
    return learner, subject, prereq, blocked


async def _detour_events(session: AsyncSession, learner: Learner) -> list[LearningEvent]:
    rows = await session.scalars(
        select(LearningEvent).where(
            LearningEvent.learner_id == learner.id,
            LearningEvent.event_type == mastery.DETOUR_EVENT,
        )
    )
    return list(rows)


async def test_taking_a_detour_is_written_to_the_event_log(db_session: AsyncSession) -> None:
    """Without this "did detouring help?" cannot be asked of the data, because there is no
    data — a detour was a plan mutation that left no trace a decision had been made."""
    learner, subject, prereq, blocked = await _stuck(db_session)

    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)

    events = await _detour_events(db_session, learner)
    assert len(events) == 1
    # Tagged to the blocked component: the question is whether detouring helped *it*.
    assert events[0].kc_id == blocked.id
    assert events[0].payload["prereq_kc_id"] == str(prereq.id)
    assert events[0].payload["consecutive_failures"] == 2


async def test_revising_again_does_not_record_a_second_detour(db_session: AsyncSession) -> None:
    """Revision runs on every graded answer, so a detour must be recorded once per trip."""
    learner, subject, _prereq, _blocked = await _stuck(db_session)

    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)

    assert len(await _detour_events(db_session, learner)) == 1


async def test_a_detour_decided_but_not_inserted_is_not_recorded(
    db_session: AsyncSession,
) -> None:
    """``revise_steps`` declines a detour whose step is already open. The decision and the
    insertion are separate outcomes, and recording the decision would put the learner on the
    record for a trip they were never sent on — and spend one of their two against the cap.

    Reaching it takes a hand-built plan, because the ordering rules normally make an open
    detour the active step and the trigger only fires on an active *new* one. That is exactly
    why the guard is here rather than left to the callers happening not to hit it.
    """
    learner, subject, prereq, blocked = await _stuck(db_session)
    plan = await svc.get_lesson_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    steps = [dict(step) for step in plan.steps]
    for step in steps:
        step["status"] = "active" if step["kc_id"] == str(blocked.id) else "pending"
    steps.append(
        {
            "kc_id": str(prereq.id),
            "order": 99,
            "step_type": "detour",
            "status": "pending",
            "target_difficulty": None,
            "hint_density": None,
            "preferred_item_type": None,
            "detour_for": str(blocked.id),
            "detour_reason": "repeated_failure",
        }
    )
    plan.steps = steps
    await db_session.flush()

    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)

    assert await _detour_events(db_session, learner) == []


async def test_the_same_prerequisite_is_not_offered_forever(db_session: AsyncSession) -> None:
    """The trigger reads the current run of failures, so nothing stopped a learner being sent
    back to a prerequisite that was not the problem after every single failed attempt."""
    learner, subject, prereq, blocked = await _stuck(db_session)
    # Two trips already taken and neither unstuck them.
    for _ in range(get_settings().detour_max_repeats):
        mastery.record_detour(
            db_session,
            learner_id=learner.id,
            blocked_kc_id=blocked.id,
            prereq_kc_id=prereq.id,
            reason="repeated_failure",
            consecutive_failures=2,
        )
    await db_session.flush()

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    assert not any(step["step_type"] == "detour" for step in revised.steps)


async def test_one_trip_short_of_the_cap_still_detours(db_session: AsyncSession) -> None:
    """The boundary in the other direction, so the cap is a cap and not an off switch."""
    learner, subject, prereq, blocked = await _stuck(db_session)
    for _ in range(get_settings().detour_max_repeats - 1):
        mastery.record_detour(
            db_session,
            learner_id=learner.id,
            blocked_kc_id=blocked.id,
            prereq_kc_id=prereq.id,
            reason="repeated_failure",
            consecutive_failures=2,
        )
    await db_session.flush()

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    assert any(step["step_type"] == "detour" for step in revised.steps)


async def test_a_detour_asks_in_a_format_that_can_say_why(db_session: AsyncSession) -> None:
    """A detour is the claim "you cannot do this because you cannot do that". Ordinary practice
    on the prerequisite does not test that claim; an answer that can be diagnosed does."""
    learner, subject, prereq, _blocked = await _stuck(db_session)

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    step = next(s for s in revised.steps if s["step_type"] == "detour")
    assert step["kc_id"] == str(prereq.id)
    assert step["preferred_item_type"] == engine.DETOUR_ITEM_TYPE
    assert step["preferred_item_type"] in {t.value for t in RUBRIC_GRADABLE}


async def test_a_format_preference_does_not_override_a_detour(db_session: AsyncSession) -> None:
    """``score_by_format`` answers "which formats does this learner do well on", which is the
    wrong question to ask of a step whose whole purpose is to find something out."""
    learner, subject, _prereq, _blocked = await _stuck(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="score_by_format",
            value={
                "flashcard": {"mean_score": 0.9, "mean_difficulty": 0.5},
                "mcq": {"mean_score": 0.5, "mean_difficulty": 0.5},
            },
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    detour_step = next(s for s in revised.steps if s["step_type"] == "detour")
    other = next(s for s in revised.steps if s["step_type"] == "new" and s["status"] != "done")
    assert detour_step["preferred_item_type"] == engine.DETOUR_ITEM_TYPE
    assert other["preferred_item_type"] == "flashcard"


# --- the learner's say, end to end (S11) -------------------------------------


async def _outcome_events(session: AsyncSession, learner: Learner) -> list[LearningEvent]:
    rows = await session.scalars(
        select(LearningEvent).where(
            LearningEvent.learner_id == learner.id,
            LearningEvent.event_type == mastery.DETOUR_OUTCOME_EVENT,
        )
    )
    return list(rows)


async def _exploring(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    learner, subject, prereq, blocked = await _stuck(session)
    await svc.set_guidance(
        session, learner_id=learner.id, subject_id=subject.id, guidance="exploration"
    )
    return learner, subject, prereq, blocked


def _detour_step(plan, prereq: KC) -> dict:
    return next(
        s for s in plan.steps if s["step_type"] == "detour" and s["kc_id"] == str(prereq.id)
    )


async def test_exploration_proposes_and_records_the_offer(db_session: AsyncSession) -> None:
    learner, subject, prereq, blocked = await _exploring(db_session)
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert _detour_step(plan, prereq)["status"] == "proposed"
    active = next(s for s in plan.steps if s["status"] == "active")
    assert active["kc_id"] == str(blocked.id)
    events = await _detour_events(db_session, learner)
    assert len(events) == 1 and events[0].payload["proposed"] is True


async def test_accepting_then_answering_well_disproves_the_gap(db_session: AsyncSession) -> None:
    learner, subject, prereq, blocked = await _exploring(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await svc.decide_detour(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        prereq_kc_id=prereq.id,
        decision="accept",
    )
    await _observe(db_session, learner, prereq, 0.9)
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    step = _detour_step(plan, prereq)
    assert (step["status"], step["detour_outcome"]) == ("done", "disproved")
    assert next(s for s in plan.steps if s["status"] == "active")["kc_id"] == str(blocked.id)
    outcomes = await _outcome_events(db_session, learner)
    assert [(e.kc_id, e.payload["outcome"]) for e in outcomes] == [(blocked.id, "disproved")]


async def test_a_pass_from_before_the_detour_does_not_disprove_it(
    db_session: AsyncSession,
) -> None:
    learner, subject, prereq, _blocked = await _stuck(db_session)
    # A good, unassisted answer on the prerequisite — an hour before the detour opens.
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={prereq.id: 1.0}, score=0.95),
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert _detour_step(plan, prereq)["status"] == "active"


async def test_an_assisted_pass_does_not_disprove_it(db_session: AsyncSession) -> None:
    learner, subject, prereq, _blocked = await _stuck(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={prereq.id: 1.0}, score=0.95, hints_used=1),
    )
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert _detour_step(plan, prereq)["status"] == "active"


async def test_a_self_rating_does_not_disprove_it(db_session: AsyncSession) -> None:
    learner, subject, prereq, _blocked = await _stuck(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={prereq.id: 1.0},
            score=1.0,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert _detour_step(plan, prereq)["status"] == "active"


async def test_a_pass_straight_after_a_worked_example_does_not_disprove_it(
    db_session: AsyncSession,
) -> None:
    # Guided practice shows a worked example before every problem, so its correct, hint-free
    # first answer is the detour working — not evidence the prerequisite was never the gap.
    learner, subject, prereq, _blocked = await _stuck(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={prereq.id: 1.0},
            score=0.95,
            hints_used=0,
            taught_first=True,
        ),
    )
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert _detour_step(plan, prereq).get("detour_outcome") != "disproved"
    assert "disproved" not in [
        e.payload["outcome"] for e in await _outcome_events(db_session, learner)
    ]


async def test_the_same_pass_without_a_worked_example_still_disproves_it(
    db_session: AsyncSession,
) -> None:
    # The control for the test above: the same correct, hint-free answer from a check or a
    # review (not taught first) is still an unaided demonstration and still disproves.
    learner, subject, prereq, blocked = await _stuck(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={prereq.id: 1.0}, score=0.95, hints_used=0),
    )
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    step = _detour_step(plan, prereq)
    assert (step["status"], step["detour_outcome"]) == ("done", "disproved")
    outcomes = await _outcome_events(db_session, learner)
    assert [(e.kc_id, e.payload["outcome"]) for e in outcomes] == [(blocked.id, "disproved")]


async def test_skipping_returns_to_the_blocked_step_and_is_remembered(
    db_session: AsyncSession,
) -> None:
    learner, subject, prereq, blocked = await _stuck(db_session)  # guided: already active
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    plan = await svc.decide_detour(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        prereq_kc_id=prereq.id,
        decision="skip",
    )
    assert plan is not None
    assert _detour_step(plan, prereq)["status"] == "skipped"
    assert next(s for s in plan.steps if s["status"] == "active")["kc_id"] == str(blocked.id)
    assert [e.payload["outcome"] for e in await _outcome_events(db_session, learner)] == ["skipped"]

    # Still failing the blocked component: the skipped route is not offered again.
    await _observe(db_session, learner, blocked, 0.1)
    await _observe(db_session, learner, blocked, 0.1)
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert not any(
        s["step_type"] == "detour" and s["status"] in ("active", "pending", "proposed")
        for s in plan.steps
    )


async def test_a_mastered_route_is_not_closed(db_session: AsyncSession) -> None:
    learner, _subject, prereq, blocked = await _stuck(db_session)
    mastery.record_detour_outcome(
        db_session,
        learner_id=learner.id,
        blocked_kc_id=blocked.id,
        prereq_kc_id=prereq.id,
        outcome="mastered",
    )
    await db_session.flush()
    assert await mastery.closed_detour_routes(db_session, learner.id, blocked.id) == set()


async def test_outcomes_are_decisions_not_evidence(db_session: AsyncSession) -> None:
    learner, subject, prereq, blocked = await _stuck(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    before = await mastery.kc_evidence(db_session, learner.id, [prereq.id, blocked.id])
    await svc.decide_detour(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        prereq_kc_id=prereq.id,
        decision="skip",
    )
    after = await mastery.kc_evidence(db_session, learner.id, [prereq.id, blocked.id])
    assert after == before
    struggle = await mastery.recent_struggle(db_session, learner.id, blocked.id, threshold=0.5)
    assert struggle.consecutive_failures == 2


async def test_switching_guidance_leaves_an_open_proposal_alone(
    db_session: AsyncSession,
) -> None:
    """Review focus 1: changing the setting does not rewrite steps (spec §2)."""
    learner, subject, prereq, _blocked = await _exploring(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    await svc.set_guidance(
        db_session, learner_id=learner.id, subject_id=subject.id, guidance="guided"
    )
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    assert _detour_step(plan, prereq)["status"] == "proposed"
    plan = await svc.decide_detour(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        prereq_kc_id=prereq.id,
        decision="accept",
    )
    assert plan is not None and _detour_step(plan, prereq)["status"] == "active"


async def test_a_new_plan_is_guided(db_session: AsyncSession) -> None:
    learner, subject, _prereq, _blocked = await _graph(db_session)
    plan = await _plan(db_session, learner, subject)
    assert plan is not None and plan.guidance == "guided"


async def test_a_route_closed_mastered_can_reopen_and_its_own_skip_is_remembered(
    db_session: AsyncSession,
) -> None:
    """Review fix round 1, finding 1: `closed_detour_routes` does not bar a route that closed
    `"mastered"` — mastery can slip and send the learner back to it. That second trip is a
    different *step* on the same route, and its own close needs its own event; keying the
    diff on the route alone let the second closure collide with the first in the dict and
    silently write nothing."""
    learner, subject, prereq, blocked = await _stuck(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)  # detour 1

    # Master the prerequisite: the open detour closes "mastered".
    state = await db_session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == prereq.id
        )
    )
    assert state is not None
    state.ability, state.uncertainty, state.last_seen_at = 2.0, 0.2, datetime.now(UTC)
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    first = _detour_step(plan, prereq)
    assert (first["status"], first["detour_outcome"]) == ("done", "mastered")
    assert await mastery.closed_detour_routes(db_session, learner.id, blocked.id) == set()

    # Mastery slips: still failing the blocked component, so the route reopens.
    state.ability, state.uncertainty = -1.0, 0.9
    await db_session.flush()
    plan = await svc.revise_plan(
        db_session, learner_id=learner.id, subject_id=subject.id
    )  # detour 2
    assert plan is not None
    detour_steps = [
        s for s in plan.steps if s["step_type"] == "detour" and s["kc_id"] == str(prereq.id)
    ]
    assert len(detour_steps) == 2
    second = next(s for s in detour_steps if s["status"] == "active")
    assert second["opened_at"] != first["opened_at"]

    plan = await svc.decide_detour(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        prereq_kc_id=prereq.id,
        decision="skip",
    )
    assert plan is not None

    outcomes = await _outcome_events(db_session, learner)
    assert {e.payload["outcome"] for e in outcomes} == {"mastered", "skipped"}
    assert len(outcomes) == 2
    assert await mastery.closed_detour_routes(db_session, learner.id, blocked.id) == {prereq.id}


async def test_two_never_accepted_proposals_on_one_route_are_remembered_separately(
    db_session: AsyncSession,
) -> None:
    """Review fix round 2, finding 1: the round-1 fix's fallback for a step with no
    `opened_at` (an occurrence index among same-route steps) was order-dependent —
    `revise_steps` re-sorts closed steps by their *previous* order, so the index a step got in
    the "before" read of `plan.steps` was not guaranteed to match the index it gets in the
    "after" read of `revised`. This is the case that actually needs it: neither proposal here
    is ever accepted, so neither ever gets an `opened_at` — only `offered_at`, stamped on
    every insertion regardless of guidance, tells the two apart."""
    learner, subject, prereq, blocked = await _exploring(db_session)
    await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)  # P1 proposed

    # Master the prerequisite without ever accepting the offer: P1 closes "mastered" still
    # `"proposed"` (revise_steps rule 1 applies to every detour status, not only open ones).
    state = await db_session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == prereq.id
        )
    )
    assert state is not None
    state.ability, state.uncertainty, state.last_seen_at = 2.0, 0.2, datetime.now(UTC)
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    first = _detour_step(plan, prereq)
    assert (first["status"], first["detour_outcome"]) == ("done", "mastered")
    assert first.get("opened_at") is None

    # Mastery slips: still failing the blocked component, so the route reopens — P2 proposed.
    state.ability, state.uncertainty = -1.0, 0.9
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert plan is not None
    detour_steps = [
        s for s in plan.steps if s["step_type"] == "detour" and s["kc_id"] == str(prereq.id)
    ]
    assert len(detour_steps) == 2
    second = next(s for s in detour_steps if s["status"] == "proposed")
    assert second.get("opened_at") is None
    assert second["offered_at"] != first["offered_at"]

    plan = await svc.decide_detour(
        db_session,
        learner_id=learner.id,
        subject_id=subject.id,
        prereq_kc_id=prereq.id,
        decision="skip",
    )
    assert plan is not None

    outcomes = await _outcome_events(db_session, learner)
    assert {e.payload["outcome"] for e in outcomes} == {"mastered", "skipped"}
    assert len(outcomes) == 2
    assert await mastery.closed_detour_routes(db_session, learner.id, blocked.id) == {prereq.id}
