"""Evidence kinds (S56): a self-rating is evidence about retention, not about ability."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import mastery
from app.learning.grading import auto_grade, grade_flashcard
from app.learning.mastery import Observation
from app.models.assessment import EvidenceKind, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent


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
    # Nothing moved, so there is no prior/posterior pair to record. Asserting their absence
    # rather than their equality: a replay must not be handed a step it can "reproduce".
    assert "posterior_ability" not in events[0].payload


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
