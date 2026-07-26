"""Dashboard analytics: on-demand mastery rollups + the activity (streak/momentum) summary.

Mirrors the ``lesson_plan.py``/``mastery.py`` split — ``app.learning.activity`` holds the
pure streak/momentum policy; this module is the I/O around it plus the mastery-rollup
plumbing over ``app.learning.mastery``'s already-tested tracer functions.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import mastery
from app.learning.activity import momentum_trend, streak_days
from app.learning.tracer import Estimate, aggregate
from app.models.knowledge import KC, Topic
from app.models.learning import LearningEvent
from app.schemas.analytics import ActivityRead, KCMasteryRead, SubjectMasteryRead, TopicMasteryRead
from app.services.lesson_plan import MASTERY_ABILITY_THRESHOLD, MASTERY_UNCERTAINTY_THRESHOLD


def _is_mastered(estimate: Estimate) -> bool:
    return (
        estimate.ability >= MASTERY_ABILITY_THRESHOLD
        and estimate.uncertainty <= MASTERY_UNCERTAINTY_THRESHOLD
    )


async def subject_mastery(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> SubjectMasteryRead:
    """Subject → topic → KC mastery, drill-down shaped (TECHNICAL_DESIGN §7.4's target UX:
    "Calculus 62% (wide)" down into "Integrals 40%, integration-by-parts weakest")."""
    now = datetime.now(UTC)
    topics = (await session.scalars(select(Topic).where(Topic.subject_id == subject_id))).all()

    topic_reads: list[TopicMasteryRead] = []
    subject_estimates: list[Estimate] = []
    subject_weights: list[float] = []
    for topic in topics:
        kcs = (await session.scalars(select(KC).where(KC.topic_id == topic.id))).all()
        if not kcs:
            continue
        kc_reads: list[KCMasteryRead] = []
        for kc in kcs:
            estimate = await mastery.estimate_kc(session, learner_id, kc.id, now=now)
            kc_reads.append(
                KCMasteryRead(
                    kc_id=kc.id,
                    kc_name=kc.name,
                    ability=estimate.ability,
                    uncertainty=estimate.uncertainty,
                    mastered=_is_mastered(estimate),
                )
            )
        topic_estimate = await mastery.rollup_topic(session, learner_id, topic.id, now=now)
        topic_reads.append(
            TopicMasteryRead(
                topic_id=topic.id,
                topic_name=topic.name,
                ability=topic_estimate.ability,
                uncertainty=topic_estimate.uncertainty,
                mastered=_is_mastered(topic_estimate),
                kcs=kc_reads,
            )
        )
        subject_estimates.append(topic_estimate)
        subject_weights.append(float(len(kcs)))

    subject_estimate = aggregate(subject_estimates, subject_weights)
    return SubjectMasteryRead(
        subject_id=subject_id,
        ability=subject_estimate.ability,
        uncertainty=subject_estimate.uncertainty,
        mastered=_is_mastered(subject_estimate),
        topics=topic_reads,
    )


async def get_activity(session: AsyncSession, learner_id: uuid.UUID) -> ActivityRead:
    """Streak (consecutive practice days) + momentum (7d-vs-prior-7d observation volume),
    both derived from the raw ``learning_events`` log — no new tables.

    ``LearningEvent.created_at`` is a naive ``TIMESTAMP`` (unlike ``LearnerKCState``'s explicit
    ``DateTime(timezone=True)`` fields) — the server writes it via Postgres's ``now()``, which is
    UTC in this deployment, so comparisons here strip tzinfo rather than attach it.
    """
    now = datetime.now(UTC)
    today = now.date()
    naive_now = now.replace(tzinfo=None)
    lookback_start = naive_now - timedelta(days=90)
    timestamps = (
        await session.scalars(
            select(LearningEvent.created_at).where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.event_type == "observation",
                LearningEvent.created_at >= lookback_start,
            )
        )
    ).all()

    active_days = {ts.date() for ts in timestamps}
    last_7d_start = naive_now - timedelta(days=7)
    prior_7d_start = naive_now - timedelta(days=14)
    observations_last_7d = sum(1 for ts in timestamps if ts >= last_7d_start)
    observations_prior_7d = sum(1 for ts in timestamps if prior_7d_start <= ts < last_7d_start)

    return ActivityRead(
        streak_days=streak_days(active_days, today),
        observations_last_7d=observations_last_7d,
        observations_prior_7d=observations_prior_7d,
        momentum=momentum_trend(observations_last_7d, observations_prior_7d),
    )
