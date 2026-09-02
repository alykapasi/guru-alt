"""Mining observation sequences from the LearningEvent log (Phase 9b)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from tests.eval.datasets.mine import mine_observation_sequences


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"mine-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kc(session: AsyncSession, name: str) -> KC:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name=name)
    session.add(kc)
    await session.flush()
    return kc


def _obs(learner_id, kc_id, score, difficulty, when, *, payload=None) -> LearningEvent:
    return LearningEvent(
        learner_id=learner_id,
        kc_id=kc_id,
        event_type="observation",
        payload=payload if payload is not None else {"score": score, "difficulty": difficulty},
        created_at=when,
    )


async def test_mine_groups_orders_and_filters(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc_long = await _kc(db_session, "long")
    kc_short = await _kc(db_session, "short")
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    # kc_long: 3 observations inserted OUT of order -> must return time-ordered
    db_session.add(_obs(learner.id, kc_long.id, 0.5, 0.0, t0 + timedelta(minutes=2)))
    db_session.add(_obs(learner.id, kc_long.id, 1.0, 0.0, t0))
    db_session.add(_obs(learner.id, kc_long.id, 0.0, 1.0, t0 + timedelta(minutes=1)))
    # kc_short: only 2 -> filtered out at min_length=3
    db_session.add(_obs(learner.id, kc_short.id, 1.0, 0.0, t0))
    db_session.add(_obs(learner.id, kc_short.id, 1.0, 0.0, t0 + timedelta(minutes=1)))
    await db_session.flush()

    dataset = await mine_observation_sequences(db_session, min_length=3)

    assert len(dataset.sequences) == 1
    seq = dataset.sequences[0]
    assert seq.kc_id == str(kc_long.id)
    assert [s.score for s in seq.steps] == [1.0, 0.0, 0.5]  # time-ordered


async def test_mine_skips_malformed_payload(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session, "kc")
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    db_session.add(_obs(learner.id, kc.id, 1.0, 0.0, t0))
    db_session.add(_obs(learner.id, kc.id, 0.5, 0.5, t0 + timedelta(minutes=1)))
    db_session.add(_obs(learner.id, kc.id, None, None, t0 + timedelta(minutes=2), payload={"x": 1}))
    db_session.add(_obs(learner.id, kc.id, 0.0, 1.0, t0 + timedelta(minutes=3)))
    await db_session.flush()

    dataset = await mine_observation_sequences(db_session, min_length=3)

    assert len(dataset.sequences) == 1
    # the malformed observation is skipped; the 3 valid ones remain, in order
    assert [s.score for s in dataset.sequences[0].steps] == [1.0, 0.5, 0.0]


async def test_mine_empty_log_returns_empty(db_session: AsyncSession) -> None:
    assert (await mine_observation_sequences(db_session)).sequences == []
