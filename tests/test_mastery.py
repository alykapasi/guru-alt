"""DB-backed tracer service: persistence, decay, multi-KC apportioning, roll-up."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import mastery
from app.learning.mastery import Observation
from app.learning.tracer import DEFAULT_UNCERTAINTY, Estimate, GlickoEstimator
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent


async def _seed(
    session: AsyncSession, *, kc_slugs: Sequence[str] = ("a",)
) -> tuple[Learner, Subject, Topic, list[KC]]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [KC(topic_id=topic.id, slug=s, name=s) for s in kc_slugs]
    session.add_all(kcs)
    await session.flush()
    return learner, subject, topic, kcs


async def _events_for(session: AsyncSession, kc_id: uuid.UUID) -> Sequence[LearningEvent]:
    return (await session.scalars(select(LearningEvent).where(LearningEvent.kc_id == kc_id))).all()


async def test_record_creates_state_and_event(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, obs)

    assert state.ability > 0.0
    assert state.uncertainty < DEFAULT_UNCERTAINTY
    assert state.last_seen_at is not None

    (event,) = await _events_for(db_session, kc.id)
    assert event.event_type == "observation"
    assert event.payload["score"] == 1.0
    assert event.payload["weight"] == 1.0
    assert event.payload["estimator"] == "glicko"


async def test_repeated_correct_sharpens(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (first,) = await mastery.record_observation(db_session, obs)
    a1, u1 = first.ability, first.uncertainty
    (second,) = await mastery.record_observation(db_session, obs)

    assert second.ability > a1
    assert second.uncertainty < u1
    assert len(await _events_for(db_session, kc.id)) == 2


async def test_wrong_answer_lowers_ability(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=0.0)
    (state,) = await mastery.record_observation(db_session, obs)
    assert state.ability < 0.0


async def test_estimate_unseen_kc_is_prior(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    est = await mastery.estimate_kc(db_session, learner.id, kc.id)
    assert est.ability == 0.0
    assert est.uncertainty == DEFAULT_UNCERTAINTY


async def test_decay_widens_uncertainty_between_sessions(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, obs, now=t0)
    sharpened = state.uncertainty

    later = await mastery.estimate_kc(db_session, learner.id, kc.id, now=t0 + timedelta(days=90))
    assert later.uncertainty > sharpened
    assert later.ability == state.ability  # decay forgets certainty, not the estimate


async def test_multi_kc_apportions_credit_by_weight(db_session: AsyncSession) -> None:
    learner, _, _, (a, b) = await _seed(db_session, kc_slugs=("a", "b"))
    obs = Observation(learner_id=learner.id, kc_weights={a.id: 1.0, b.id: 1.0}, score=1.0)
    states = {s.kc_id: s for s in await mastery.record_observation(db_session, obs)}

    # Equal weights ⇒ symmetric, and each KC got exactly a weight-0.5 update.
    expected = GlickoEstimator().update(Estimate(), score=1.0, difficulty=0.0, weight=0.5)
    assert states[a.id].ability == states[b.id].ability == expected.ability
    # ...which is a smaller move than a full single-KC update would have been.
    full = GlickoEstimator().update(Estimate(), score=1.0, difficulty=0.0, weight=1.0)
    assert states[a.id].ability < full.ability


async def test_rollup_topic_widened_by_untested_kc(db_session: AsyncSession) -> None:
    learner, _, topic, (a, _b) = await _seed(db_session, kc_slugs=("a", "b"))
    # Master `a` with several correct answers; leave `b` untested.
    obs = Observation(learner_id=learner.id, kc_weights={a.id: 1.0}, score=1.0)
    for _ in range(5):
        await mastery.record_observation(db_session, obs)

    a_est = await mastery.estimate_kc(db_session, learner.id, a.id)
    topic_est = await mastery.rollup_topic(db_session, learner.id, topic.id)

    assert topic_est.ability > 0.5  # dominated by what we measured on `a`
    assert a_est.uncertainty < topic_est.uncertainty < DEFAULT_UNCERTAINTY  # `b` widens it


async def test_rollup_empty_topic_is_prior(db_session: AsyncSession) -> None:
    learner, _, topic, _ = await _seed(db_session, kc_slugs=())
    est = await mastery.rollup_topic(db_session, learner.id, topic.id)
    assert est.ability == 0.0
    assert est.uncertainty == DEFAULT_UNCERTAINTY


async def test_rollup_subject_weights_topics_by_kc_count(db_session: AsyncSession) -> None:
    # One subject, two topics: a 1-KC topic (strong) and a 3-KC topic (weak). The subject
    # should be pulled toward the larger (more-KC) topic.
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    strong = Topic(subject_id=subject.id, slug="strong", name="strong")
    weak = Topic(subject_id=subject.id, slug="weak", name="weak")
    db_session.add_all([strong, weak])
    await db_session.flush()
    strong_kc = KC(topic_id=strong.id, slug="sk", name="sk")
    weak_kcs = [KC(topic_id=weak.id, slug=f"wk{i}", name=f"wk{i}") for i in range(3)]
    db_session.add_all([strong_kc, *weak_kcs])
    await db_session.flush()

    # Equal evidence per KC (same count, same difficulty) ⇒ equal uncertainty, so the
    # subject mean is driven purely by the KC-count weighting.
    for _ in range(4):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={strong_kc.id: 1.0}, score=1.0),
        )
        for wk in weak_kcs:
            await mastery.record_observation(
                db_session,
                Observation(learner_id=learner.id, kc_weights={wk.id: 1.0}, score=0.0),
            )

    strong_est = await mastery.rollup_topic(db_session, learner.id, strong.id)
    weak_est = await mastery.rollup_topic(db_session, learner.id, weak.id)
    subject_est = await mastery.rollup_subject(db_session, learner.id, subject.id)

    assert weak_est.ability < subject_est.ability < strong_est.ability
    midpoint = (strong_est.ability + weak_est.ability) / 2
    assert subject_est.ability < midpoint  # 3 weak KCs outweigh 1 strong KC


# --- FSRS retention scheduling ----------------------------------------------


async def test_record_schedules_fsrs_review(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, obs, now=t0)
    assert state.fsrs_card is not None  # opaque card persisted
    assert state.due_at is not None and state.due_at > t0  # scheduled into the future


async def test_due_reviews_lists_only_past_due(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, obs, now=t0)

    assert await mastery.due_reviews(db_session, learner.id, now=t0) == []  # not due yet

    assert state.due_at is not None
    when_due = state.due_at + timedelta(seconds=1)
    due = await mastery.due_reviews(db_session, learner.id, now=when_due)
    assert [r.kc_id for r in due] == [kc.id]
    assert due[0].ability == state.ability


async def test_due_reviews_orders_soonest_first(db_session: AsyncSession) -> None:
    learner, _, _, (a, b) = await _seed(db_session, kc_slugs=("a", "b"))
    t0 = datetime.now(UTC)
    # `a` recalled perfectly (long interval); `b` forgotten (short interval) ⇒ b due first.
    await mastery.record_observation(
        db_session, Observation(learner_id=learner.id, kc_weights={a.id: 1.0}, score=1.0), now=t0
    )
    await mastery.record_observation(
        db_session, Observation(learner_id=learner.id, kc_weights={b.id: 1.0}, score=0.0), now=t0
    )
    due = await mastery.due_reviews(db_session, learner.id, now=t0 + timedelta(days=400))
    assert [r.kc_id for r in due] == [b.id, a.id]


# --- the KnowledgeTracer seam -----------------------------------------------


def test_default_tracer_satisfies_protocol() -> None:
    assert isinstance(mastery.DEFAULT_TRACER, mastery.KnowledgeTracer)


async def test_tracer_facade_update_estimate_and_due(db_session: AsyncSession) -> None:
    learner, _, _, (kc,) = await _seed(db_session)
    obs = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.DEFAULT_TRACER.update(db_session, obs)
    assert state.due_at is not None

    est = await mastery.DEFAULT_TRACER.estimate(db_session, learner.id, kc.id)
    assert est.ability == state.ability
