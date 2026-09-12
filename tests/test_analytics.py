"""Dashboard analytics: mastery rollup + activity (streak/momentum) summary."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.activity import momentum_trend, streak_days
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.services import analytics as svc

API = "/api/v1"


async def _subject_with_kc(session: AsyncSession) -> tuple[Learner, Subject, Topic, KC]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="Atoms")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="protons", name="Protons")
    session.add(kc)
    await session.flush()
    return learner, subject, topic, kc


# --- subject_mastery ---------------------------------------------------------


async def test_subject_mastery_empty_subject_is_unknown_prior(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Empty")
    db_session.add_all([learner, subject])
    await db_session.flush()

    result = await svc.subject_mastery(db_session, learner.id, subject.id)
    assert result.ability == 0.0
    assert result.uncertainty == 1.0
    assert result.mastered is False
    assert result.topics == []


async def test_subject_mastery_unseen_kc_is_unmastered_prior(db_session: AsyncSession) -> None:
    learner, subject, topic, kc = await _subject_with_kc(db_session)

    result = await svc.subject_mastery(db_session, learner.id, subject.id)
    assert len(result.topics) == 1
    assert result.topics[0].topic_id == topic.id
    assert len(result.topics[0].kcs) == 1
    kc_read = result.topics[0].kcs[0]
    assert kc_read.kc_id == kc.id
    assert kc_read.kc_name == "Protons"
    assert kc_read.ability == 0.0
    assert kc_read.mastered is False
    # The prior renders as 50%. Saying so is the difference between a number and a claim.
    assert kc_read.assessed is False
    assert (result.assessed_kcs, result.total_kcs) == (0, 1)
    assert (result.topics[0].assessed_kcs, result.topics[0].total_kcs) == (0, 1)


async def test_a_component_with_evidence_behind_it_is_marked_assessed(
    db_session: AsyncSession,
) -> None:
    learner, subject, _topic, kc = await _subject_with_kc(db_session)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=0.3, uncertainty=0.5))
    await db_session.flush()

    result = await svc.subject_mastery(db_session, learner.id, subject.id)
    assert result.topics[0].kcs[0].assessed is True
    assert (result.assessed_kcs, result.total_kcs) == (1, 1)


async def test_coverage_counts_only_the_components_that_were_assessed(
    db_session: AsyncSession,
) -> None:
    """A subject half-assessed must not read the same as one fully assessed."""
    learner, subject, topic, kc = await _subject_with_kc(db_session)
    other = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Neutrons")
    db_session.add(other)
    await db_session.flush()
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=0.3, uncertainty=0.5))
    await db_session.flush()

    result = await svc.subject_mastery(db_session, learner.id, subject.id)
    assert (result.assessed_kcs, result.total_kcs) == (1, 2)
    assert {k.kc_name: k.assessed for k in result.topics[0].kcs} == {
        "Protons": True,
        "Neutrons": False,
    }


async def test_subject_mastery_flags_mastered_at_every_level(db_session: AsyncSession) -> None:
    learner, subject, _topic, kc = await _subject_with_kc(db_session)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=1.5, uncertainty=0.2))
    await db_session.flush()

    result = await svc.subject_mastery(db_session, learner.id, subject.id)
    assert result.topics[0].kcs[0].mastered is True
    assert result.topics[0].mastered is True
    assert result.mastered is True


async def test_subject_mastery_skips_topics_with_no_kcs(db_session: AsyncSession) -> None:
    learner, subject, _topic, _kc = await _subject_with_kc(db_session)
    empty_topic = Topic(subject_id=subject.id, slug="empty", name="Empty Topic")
    db_session.add(empty_topic)
    await db_session.flush()

    result = await svc.subject_mastery(db_session, learner.id, subject.id)
    assert len(result.topics) == 1


# --- get_activity ------------------------------------------------------------


async def test_get_activity_no_events_is_zeroed(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    result = await svc.get_activity(db_session, learner.id)
    assert result.streak_days == 0
    assert result.observations_last_7d == 0
    assert result.observations_prior_7d == 0
    assert result.momentum == "none"


async def test_get_activity_counts_recent_vs_prior_window(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    # LearningEvent.created_at is a naive TIMESTAMP column (see get_activity's docstring) —
    # asyncpg rejects a tz-aware value outright, so seed with naive UTC-equivalent instants.
    now = datetime.now(UTC).replace(tzinfo=None)

    for offset_days in (0, 1, 2):
        db_session.add(
            LearningEvent(
                learner_id=learner.id,
                event_type="observation",
                payload={},
                created_at=now - timedelta(days=offset_days),
            )
        )
    for offset_days in (10, 11):
        db_session.add(
            LearningEvent(
                learner_id=learner.id,
                event_type="observation",
                payload={},
                created_at=now - timedelta(days=offset_days),
            )
        )
    await db_session.flush()

    result = await svc.get_activity(db_session, learner.id)
    assert result.observations_last_7d == 3
    assert result.observations_prior_7d == 2
    assert result.momentum == "up"
    assert result.streak_days == 3


async def test_get_activity_ignores_non_observation_events(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    db_session.add(LearningEvent(learner_id=learner.id, event_type="placement_seed", payload={}))
    await db_session.flush()

    result = await svc.get_activity(db_session, learner.id)
    assert result.observations_last_7d == 0


# --- pure policy: streak_days / momentum_trend -------------------------------


def test_streak_days_counts_consecutive_days_ending_today() -> None:
    today = datetime(2026, 1, 10, tzinfo=UTC).date()
    active = {today, today - timedelta(days=1), today - timedelta(days=2)}
    assert streak_days(active, today) == 3


def test_streak_days_today_not_yet_active_still_counts_from_yesterday() -> None:
    today = datetime(2026, 1, 10, tzinfo=UTC).date()
    active = {today - timedelta(days=1), today - timedelta(days=2)}
    assert streak_days(active, today) == 2


def test_streak_days_gap_breaks_the_streak() -> None:
    today = datetime(2026, 1, 10, tzinfo=UTC).date()
    active = {today - timedelta(days=2)}  # yesterday missing
    assert streak_days(active, today) == 0


def test_momentum_trend_thresholds() -> None:
    assert momentum_trend(0, 0) == "none"
    assert momentum_trend(5, 0) == "up"
    assert momentum_trend(10, 5) == "up"
    assert momentum_trend(5, 10) == "down"
    assert momentum_trend(5, 5) == "steady"


# --- HTTP level ---------------------------------------------------------


async def test_mastery_endpoint_round_trip(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    r = await api_client.post(f"{API}/subjects", json={"slug": "phys", "name": "Physics"})
    subject_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": "t", "name": "T"}
    )
    topic_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": "a-root", "name": "A Root"}
    )
    kc_id = r.json()["id"]

    r = await api_client.get(f"{API}/subjects/{subject_id}/mastery")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject_id"] == subject_id
    assert body["topics"][0]["topic_id"] == topic_id
    assert body["topics"][0]["kcs"][0]["kc_id"] == kc_id
    assert body["topics"][0]["kcs"][0]["mastered"] is False

    r = await api_client.get(f"{API}/subjects/{uuid.uuid4()}/mastery")
    assert r.status_code == 404


async def test_activity_endpoint_reflects_seeded_events(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    # Events attached to the learner the client is signed in as, so the endpoint sees them.
    db_session.add(LearningEvent(learner_id=api_learner.id, event_type="observation", payload={}))
    await db_session.flush()

    r = await api_client.get(f"{API}/activity")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["observations_last_7d"] == 1
    assert body["streak_days"] == 1
    assert body["momentum"] == "up"


async def test_activity_counts_a_multi_kc_answer_once(db_session: AsyncSession) -> None:
    """S45: one answer tagged to three KCs is one attempt, not three observations.

    Otherwise momentum and streak reward broad KC tagging rather than learner effort.
    """
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    attempt = uuid.uuid4()

    for _ in range(3):  # the per-KC fan-out of ONE graded answer
        db_session.add(
            LearningEvent(
                learner_id=learner.id,
                event_type="observation",
                attempt_id=attempt,
                payload={"score": 1.0},
                created_at=now - timedelta(days=1),
            )
        )
    await db_session.flush()

    result = await svc.get_activity(db_session, learner.id)

    assert result.observations_last_7d == 1


async def test_activity_still_counts_legacy_events_without_an_attempt_id(
    db_session: AsyncSession,
) -> None:
    """Rows predating the attempt_id column each stand alone rather than collapsing to one."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)

    for _ in range(2):
        db_session.add(
            LearningEvent(
                learner_id=learner.id,
                event_type="observation",
                attempt_id=None,
                payload={"score": 1.0},
                created_at=now - timedelta(days=1),
            )
        )
    await db_session.flush()

    result = await svc.get_activity(db_session, learner.id)

    assert result.observations_last_7d == 2
