"""note_format dimension: earned from real explicit format choices, never fabricated."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.profile_estimators import DIMENSION_SPECS, EstimatorContext, _estimate_note_format
from app.llm.registry import fake_llm_client
from app.models.knowledge import Subject, Topic
from app.models.learner import Learner
from app.models.note import Note


def _ctx(session: AsyncSession, learner_id: uuid.UUID) -> EstimatorContext:
    return EstimatorContext(
        session=session, learner_id=learner_id, events=[], messages=[], llm=fake_llm_client()
    )


async def _learner_with_notes(db_session: AsyncSession, formats: list[str | None]) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    for fmt in formats:
        topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
        db_session.add(topic)
        await db_session.flush()
        db_session.add(Note(learner_id=learner.id, topic_id=topic.id, format=fmt))
    await db_session.flush()
    return learner


async def test_registered_in_catalog() -> None:
    assert any(spec.key == "note_format" for spec in DIMENSION_SPECS)


async def test_under_threshold_emits_nothing(db_session: AsyncSession) -> None:
    learner = await _learner_with_notes(db_session, ["outline", "outline"])
    estimate, usage = await _estimate_note_format(_ctx(db_session, learner.id))
    assert estimate is None and usage.input_tokens == 0


async def test_majority_format_emitted(db_session: AsyncSession) -> None:
    learner = await _learner_with_notes(db_session, ["narrative", "narrative", "outline", None])
    estimate, _ = await _estimate_note_format(_ctx(db_session, learner.id))
    assert estimate is not None
    assert estimate.value == "narrative"
    assert 0.0 <= estimate.uncertainty < 1.0


async def test_no_majority_emits_nothing(db_session: AsyncSession) -> None:
    learner = await _learner_with_notes(
        db_session, ["narrative", "outline", "mnemonic", "worked_examples"]
    )
    estimate, _ = await _estimate_note_format(_ctx(db_session, learner.id))
    assert estimate is None
