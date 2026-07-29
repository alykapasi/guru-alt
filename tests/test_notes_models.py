"""Note/NoteRevision/NoteRender: model round-trips + constraints."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject, Topic
from app.models.learner import Learner
from app.models.note import WATERMARK_EPOCH, Note, NoteRender, NoteRevision


async def _learner_topic(db_session: AsyncSession) -> tuple[Learner, Topic]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    db_session.add(topic)
    await db_session.flush()
    return learner, topic


async def test_note_round_trip_with_revision_and_render(db_session: AsyncSession) -> None:
    learner, topic = await _learner_topic(db_session)
    atoms = [{"id": "a-1", "kind": "concept", "kc_ids": [], "md": "Vectors add.", "provenance": {}}]
    note = Note(learner_id=learner.id, topic_id=topic.id, substrate=atoms, revision_ordinal=1)
    db_session.add(note)
    await db_session.flush()
    assert note.watermark == WATERMARK_EPOCH
    assert note.format is None

    db_session.add(NoteRevision(note_id=note.id, ordinal=1, substrate=atoms, cause="distill"))
    db_session.add(
        NoteRender(
            note_id=note.id, revision_ordinal=1, format="outline", content_md="- Vectors add."
        )
    )
    await db_session.flush()

    fetched = await db_session.get(Note, note.id)
    assert fetched is not None
    assert fetched.substrate[0]["md"] == "Vectors add."


async def test_one_note_per_learner_topic(db_session: AsyncSession) -> None:
    learner, topic = await _learner_topic(db_session)
    db_session.add(Note(learner_id=learner.id, topic_id=topic.id))
    await db_session.flush()
    db_session.add(Note(learner_id=learner.id, topic_id=topic.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
