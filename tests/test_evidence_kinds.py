"""Evidence kinds (S56): a self-rating is evidence about retention, not about ability."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import mastery
from app.learning.grading import auto_grade, grade_flashcard
from app.learning.mastery import Observation
from app.learning.tracer import Estimate
from app.models.assessment import EvidenceKind, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import lesson_plan as lesson_plan_svc


def test_a_self_rated_flashcard_is_marked_self_reported() -> None:
    assert grade_flashcard({"rating": 4}).evidence_kind is EvidenceKind.SELF_REPORTED


def test_a_deterministically_graded_answer_is_marked_demonstrated() -> None:
    result = auto_grade(ItemType.MCQ, {"correct": 1, "choices": ["a", "b"]}, {"choice": 1})
    assert result.evidence_kind is EvidenceKind.DEMONSTRATED


def test_an_observation_is_demonstrated_unless_it_says_otherwise() -> None:
    """The default is the safe one: a caller that forgets the field asserts nothing extra.

    Inverted, a forgotten field would silently downgrade real evidence to self-report and
    stop the tracer learning from it — a failure that looks like nothing at all.
    """
    obs = Observation(learner_id=uuid.uuid4(), kc_weights={uuid.uuid4(): 1.0}, score=1.0)
    assert obs.evidence_kind is EvidenceKind.DEMONSTRATED


def test_a_client_cannot_claim_its_answer_was_demonstrated() -> None:
    """The kind is derived, never accepted. Pydantic ignores unknown fields by default, so
    without this test a future `model_config = {"extra": "allow"}` would silently hand the
    browser control of whether its own rating counts as evidence.
    """
    from app.schemas.assessment import AnswerSubmit

    submission = AnswerSubmit.model_validate(
        {"response": {"rating": 4}, "evidence_kind": "demonstrated"}
    )
    assert not hasattr(submission, "evidence_kind")


# --- the tracer branch (S56) ---------------------------------------------------


async def _seed(session: AsyncSession, *, n: int = 1) -> tuple[Learner, list[KC]]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [KC(topic_id=topic.id, slug=f"kc{i}", name=f"kc{i}") for i in range(n)]
    session.add_all(kcs)
    await session.flush()
    return learner, kcs


async def test_a_self_rating_advances_the_schedule_and_moves_nothing_else(
    db_session: AsyncSession,
) -> None:
    """The whole point, stated once: retention yes, ability no."""
    learner, (kc,) = await _seed(db_session)
    graded = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, graded)
    ability, uncertainty = state.ability, state.uncertainty
    last_seen, due = state.last_seen_at, state.due_at

    rated = Observation(
        learner_id=learner.id,
        kc_weights={kc.id: 1.0},
        score=1.0,
        evidence_kind=EvidenceKind.SELF_REPORTED,
    )
    (after,) = await mastery.record_observation(
        db_session, rated, now=datetime.now(UTC) + timedelta(days=1)
    )

    assert after.ability == ability
    assert after.uncertainty == uncertainty
    assert after.last_seen_at == last_seen
    assert after.due_at != due  # the review schedule did move


async def test_a_graded_answer_still_moves_everything(db_session: AsyncSession) -> None:
    """The other half of the claim — without this, deleting the update would also pass."""
    learner, (kc,) = await _seed(db_session)
    first = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, first)
    ability, uncertainty = state.ability, state.uncertainty
    last_seen, due = state.last_seen_at, state.due_at

    second = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (after,) = await mastery.record_observation(
        db_session, second, now=datetime.now(UTC) + timedelta(days=1)
    )

    assert after.ability != ability
    assert after.uncertainty != uncertainty
    assert after.last_seen_at != last_seen
    assert after.due_at != due


async def test_self_report_does_not_keep_a_stale_estimate_looking_fresh(
    db_session: AsyncSession,
) -> None:
    """The regression a single-rating test would miss.

    `last_seen_at` drives `elapsed_days` -> decay -> uncertainty growth. If a self-rating
    refreshed it, a learner could rate daily for a month and keep a month-old estimate
    reading as current — the same contamination as moving ability, just slower.
    """
    learner, (kc,) = await _seed(db_session)
    start = datetime.now(UTC) - timedelta(days=30)
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
        now=start,
    )
    for day in range(1, 30):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
            now=start + timedelta(days=day),
        )
    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None
    assert state.last_seen_at is not None
    # Still the day of the one real demonstration, not day 29.
    assert (state.last_seen_at.replace(tzinfo=UTC) - start).days == 0


async def test_a_self_rating_writes_its_own_event_type(db_session: AsyncSession) -> None:
    learner, (kc,) = await _seed(db_session)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=0.8,
            detail={"rating": 3, "method": "self"},
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    events = (
        await db_session.scalars(select(LearningEvent).where(LearningEvent.kc_id == kc.id))
    ).all()
    assert [e.event_type for e in events] == [mastery.SELF_REPORT_EVENT]
    assert events[0].payload["score"] == 0.8
    assert events[0].payload["detail"]["rating"] == 3
    # Nothing moved, so none of the replay/update machinery belongs in this payload. A
    # set-difference over every forbidden key, not just one: asserting only
    # `posterior_ability`'s absence would pass even if a regression re-added the other eight.
    forbidden = {
        "credit",
        "estimator",
        "estimator_config",
        "elapsed_days",
        "predicted",
        "prior_ability",
        "prior_uncertainty",
        "posterior_ability",
        "posterior_uncertainty",
    }
    assert forbidden.isdisjoint(events[0].payload)


async def test_a_kc_whose_first_contact_is_a_flashcard_has_no_ability_evidence(
    db_session: AsyncSession,
) -> None:
    """There must still be a row — the FSRS card needs somewhere to live — but it holds the
    unknown prior and no evidence clock."""
    learner, (kc,) = await _seed(db_session)
    (state,) = await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    assert state.ability == 0.0
    assert state.uncertainty == 1.0
    assert state.last_seen_at is None
    assert state.fsrs_card is not None
    assert state.due_at is not None


# --- the sites that opt back in (S56) -------------------------------------------


async def test_a_self_rating_counts_as_having_seen_the_item(db_session: AsyncSession) -> None:
    """Re-asks are re-asks whoever marked them: the question is "have they just seen this",
    and a learner who rated a card five minutes ago has."""
    learner, (kc,) = await _seed(db_session)
    item_id = uuid.uuid4()
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            item_id=item_id,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    count = await mastery.recent_attempts_at_item(db_session, learner.id, item_id)
    assert count == 1


async def test_repeated_low_self_ratings_still_read_as_struggle(
    db_session: AsyncSession,
) -> None:
    """A self-rating may ask for help even though it may not make a claim. Three "Again"s
    are a learner saying they are stuck, and a detour is help, not a measurement."""
    learner, (kc,) = await _seed(db_session)
    now = datetime.now(UTC)
    for day in range(3):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=0.2,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
            now=now + timedelta(days=day),
        )
    struggle = await mastery.recent_struggle(db_session, learner.id, kc.id, threshold=0.5)
    assert struggle.consecutive_failures == 3


async def test_self_ratings_are_counted_beside_the_evidence_not_inside_it(
    db_session: AsyncSession,
) -> None:
    """The assertion that stops a self-rating being presented as backing for an estimate."""
    learner, (kc,) = await _seed(db_session)
    for _ in range(3):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                item_id=uuid.uuid4(),
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
        )
    evidence = await mastery.kc_evidence(db_session, learner.id, [kc.id])
    assert evidence[kc.id].attempts == 0
    assert evidence[kc.id].distinct_items == 0
    assert evidence[kc.id].unassisted_items == 0
    assert evidence[kc.id].self_reported_attempts == 3


async def test_the_span_starts_at_a_demonstrated_attempt_not_a_self_rating(
    db_session: AsyncSession,
) -> None:
    """A mixed history, because an all-self-rated or all-demonstrated one can't expose this:
    a self-rating first, then a demonstrated attempt 9 days later. The span must start at the
    demonstrated attempt, not the self-rating — a span that started at the self-rating would
    report 9 days of retention the learner never actually demonstrated.
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
        now=t0,
    )
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
        now=t0 + timedelta(days=9),
    )
    evidence = await mastery.kc_evidence(db_session, learner.id, [kc.id])
    assert evidence[kc.id].unassisted_span_days is None
    assert evidence[kc.id].retention_shown(min_days=1.0) is False


# --- three consumers that never had to change (S56) -----------------------------


def test_the_profile_estimators_ignore_self_rated_attempts() -> None:
    """Every profile dimension reads `score` or `difficulty` — optimal challenge, pace,
    cognitive load, error types. Inferring "this learner thrives at difficulty 0.7" from
    scores the learner assigned themselves is circular.

    No production code implements this: it falls out of self-report having its own event
    type. That is exactly why it needs a test — behaviour nobody wrote is behaviour nobody
    notices breaking.
    """
    from app.learning.profile_estimators import _observations

    learner_id = uuid.uuid4()
    events = [
        LearningEvent(
            learner_id=learner_id,
            event_type="observation",
            attempt_id=uuid.uuid4(),
            payload={"score": 1.0},
        ),
        LearningEvent(
            learner_id=learner_id,
            event_type=mastery.SELF_REPORT_EVENT,
            attempt_id=uuid.uuid4(),
            payload={"score": 1.0},
        ),
    ]
    assert [e.event_type for e in _observations(events)] == ["observation"]


async def test_the_replay_miner_skips_self_rated_steps(db_session: AsyncSession) -> None:
    """A self-report row records no estimator, no prior and no posterior, because none ran.
    A replay handed one would be reproducing a step that never happened."""
    from tests.eval.datasets.mine import mine_observation_sequences

    learner, (kc,) = await _seed(db_session)
    for _ in range(4):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
        )
    dataset = await mine_observation_sequences(db_session, min_length=3)
    assert all(str(kc.id) != seq.kc_id for seq in dataset.sequences)


async def test_a_self_rating_contributes_no_diagnosis(db_session: AsyncSession) -> None:
    """Two independent things keep a self-rating out of the failure-kind counts that pick
    teaching moves, and this pins the pair.

    `grade_flashcard` returns no diagnosis, so there is nothing to count in the first place —
    a property of the grader, not of this slice. And `prior_failure_kinds` filters
    `event_type == "observation"`, so a flashcard that one day *did* carry a diagnosis would
    still be excluded. Only both giving way at once turns this assertion red, which is
    exactly why it is worth keeping: either guard alone looks removable to someone who has
    not noticed the other.
    """
    learner, (kc,) = await _seed(db_session)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=0.2,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    assert await mastery.prior_failure_kinds(db_session, learner.id, [kc.id]) == {}


# --- the cursor in front of those estimators (S56) -------------------------------


async def test_a_self_rating_does_not_force_a_profile_recompute(db_session: AsyncSession) -> None:
    """`latest_evidence_at` is the freshness cursor deciding whether `refresh_profile` does any
    work at all, and it had no event_type filter of any kind.

    Unfiltered, a self-rating advanced it and bought a full pass over DIMENSION_SPECS — the
    model-backed error-type classifier included, plus `_revise_lesson_plans` — over a history
    the estimators provably ignore (test_the_profile_estimators_ignore_self_rated_attempts).
    Bounded rather than a loop, since the cursor is set to the newest row afterwards, but one
    wasted recompute per review batch is still one nobody asked for.
    """
    from app.services import profile as profile_svc

    learner, (kc,) = await _seed(db_session)
    graded_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    db_session.add_all(
        [
            LearningEvent(
                learner_id=learner.id,
                kc_id=kc.id,
                event_type="observation",
                attempt_id=uuid.uuid4(),
                payload={"score": 1.0},
                created_at=graded_at,
            ),
            LearningEvent(
                learner_id=learner.id,
                kc_id=kc.id,
                event_type=mastery.SELF_REPORT_EVENT,
                attempt_id=uuid.uuid4(),
                payload={"score": 1.0},
                created_at=graded_at + timedelta(days=1),
            ),
        ]
    )
    await db_session.flush()

    assert await profile_svc.latest_evidence_at(db_session, learner.id) == graded_at


# --- per-component achievement (S01) ---------------------------------------------


async def test_a_self_rating_never_records_an_achievement(db_session: AsyncSession) -> None:
    """A rating moves the review schedule and nothing else (S56).

    Two independent things keep a rating out of `achieved_at`, and this test cannot tell them
    apart. The exclusion is structural: the self-report branch returns before the ability
    assignment, so the achievement check placed after it is unreachable from a rating at all.
    It is also independently blocked one layer down even if that structure were removed: a
    self-report writes a `self_report` event, not `observation`, and `kc_evidence`'s retention
    counters only count the latter, so `retention_shown` would still read false. Confirmed by
    mutation — moving the append into the self-report branch leaves this test passing, because
    the second guard alone is enough.
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    for day in (0, 30):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
            now=t0 + timedelta(days=day),
        )

    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None and state.achieved_at is None


_BUILD_UP = (*(i * 0.001 for i in range(11)), 1.0)
"""Offsets in days: eleven successes minutes apart, then one a day later (see below)."""


def _conservative(state: mastery.LearnerKCState) -> float:
    return Estimate(ability=state.ability, uncertainty=state.uncertainty).conservative


async def test_an_achievement_survives_the_estimate_falling(db_session: AsyncSession) -> None:
    """Current confidence and historical achievement are different claims.

    V0_DECISIONS asks for both and says a historical achievement does not promise permanent
    knowledge — but it also does not stop having happened.
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    # Eleven unaided successes minutes apart, then a twelfth a day later. Two things have to
    # be true on the same call for this to be a real test of the achievement check's
    # placement: the conservative estimate must clear `mastery_conservative_bar` (0.5) — at
    # two deviations, twelve straight successes on a medium item is the smallest count that
    # gets there, confirmed against the live estimator, not assumed — and the retention span
    # (first unaided attempt to last) must first clear `retention_min_days` on that *same*
    # call. Spacing the first eleven within fractions of a day keeps the span under a day
    # until the last attempt lands it there, so the event that completes the span is the very
    # one still pending until this call's own flush — exactly the ordering the achievement
    # check depends on.
    for offset in _BUILD_UP:
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
            now=t0 + timedelta(days=offset),
        )
    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None
    earned = state.achieved_at
    assert earned is not None
    # `now` pinned to the last observation each time, so decay is the identity and the counts
    # depend on the evidence rather than on how long ago this test's `t0` was.
    before = await lesson_plan_svc.goal_status(
        db_session,
        learner_id=learner.id,
        objective_kc_ids=[str(kc.id)],
        closed_at=None,
        now=t0 + timedelta(days=1.0),
    )
    assert (before.achieved_kc_count, before.current_kc_count) == (1, 1)

    for day in range(32, 44):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=0.0),
            now=t0 + timedelta(days=day),
        )

    await db_session.refresh(state)
    assert state.achieved_at == earned, "an achievement is not revoked by later evidence"
    assert _conservative(state) < get_settings().mastery_conservative_bar
    after = await lesson_plan_svc.goal_status(
        db_session,
        learner_id=learner.id,
        objective_kc_ids=[str(kc.id)],
        closed_at=None,
        now=t0 + timedelta(days=43),
    )
    # The current count dropped and the historical one did not: the two claims, side by side.
    assert (after.achieved_kc_count, after.current_kc_count) == (1, 0)


async def test_a_demonstrated_component_that_never_clears_the_bar_stays_unachieved(
    db_session: AsyncSession,
) -> None:
    """Retention alone is not achievement — the conservative estimate still has to clear the bar.

    Two unaided attempts a day apart already satisfy `retention_shown` (2 attempts, 1-day
    span), so this isolates the other half of the rule: the estimate itself. Confirmed against
    the live estimator, not assumed — two observations at score 1.0 land ability/uncertainty at
    conservative ≈ -0.15, well under `mastery_conservative_bar` (0.5).
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    for day in (0, 1):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
            now=t0 + timedelta(days=day),
        )
    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None
    assert _conservative(state) < get_settings().mastery_conservative_bar
    assert state.achieved_at is None


async def test_an_achievement_keeps_its_original_date_through_a_later_recovery(
    db_session: AsyncSession,
) -> None:
    """The `pending` filter in `_record_achievements`, exercised directly.

    An estimate that falls and then recovers above the bar a second time must not move
    `achieved_at` to the recovery date — the fact already happened once. In the "falls and
    stays down" test above, the bar check alone would block a re-stamp with no help from the
    filter; recovering *back above* the bar is the case the filter actually exists for, so this
    asserts the original date survives a second crossing, not just a fall.
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    for offset in _BUILD_UP:
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
            now=t0 + timedelta(days=offset),
        )
    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None
    earned = state.achieved_at
    assert earned is not None

    for day in (31, 32, 33):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=0.0),
            now=t0 + timedelta(days=day),
        )
    await db_session.refresh(state)
    assert _conservative(state) < get_settings().mastery_conservative_bar

    for day in range(34, 44):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
            now=t0 + timedelta(days=day),
        )
    await db_session.refresh(state)
    assert _conservative(state) >= get_settings().mastery_conservative_bar, (
        "the scenario must actually re-cross the bar, or the filter is not being exercised"
    )
    assert state.achieved_at == earned, "a second crossing is not a second achievement"
