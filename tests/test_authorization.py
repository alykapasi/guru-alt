"""What one learner can reach of another's (S21).

Threading ``learner_id`` everywhere is what made real auth a swap rather than a rewire. It is
not, by itself, evidence that the id is *used* to decide anything: with one dev learner for
every caller, a route that forgot to scope by owner behaved identically to one that did not,
and nothing in the suite could tell them apart. These tests are what tells them apart.

Every case is the same shape — B holds a live session, and asks for something of A's by id —
so a route that grows a new way to address a resource has an obvious place to be checked.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind
from app.models.note import Note
from app.models.source import Source, SourceKind, SourceStatus
from tests.conftest import sign_in
from tests.embedding import FAKE_SPACE

API = "/api/v1"
EMBED_DIM = 768

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def other_learner(db_session: AsyncSession) -> Learner:
    """Somebody who is not the learner `api_client` is signed in as."""
    learner = Learner(handle=f"other-{uuid.uuid4().hex[:8]}", display_name="Somebody Else")
    db_session.add(learner)
    await db_session.flush()
    return learner


@pytest.fixture
async def other_client(
    anon_client: AsyncClient, db_session: AsyncSession, other_learner: Learner
) -> AsyncClient:
    """A second authenticated client, signed in as `other_learner`."""
    await sign_in(anon_client, db_session, other_learner)
    return anon_client


async def _subject_topic_kc(session: AsyncSession) -> tuple[Subject, Topic, KC]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="Acids")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="pH")
    session.add(kc)
    await session.flush()
    return subject, topic, kc


# --- conversations ---------------------------------------------------------------------------


async def test_a_conversation_is_not_listed_to_anybody_else(
    api_client: AsyncClient, other_client: AsyncClient, api_learner: Learner
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "Mine"})
    assert r.status_code == 201
    listed = await other_client.get(f"{API}/conversations")
    assert listed.status_code == 200
    assert r.json()["id"] not in [c["id"] for c in listed.json()]


async def test_another_learners_conversation_cannot_be_read_by_id(
    api_client: AsyncClient, other_client: AsyncClient
) -> None:
    created = await api_client.post(f"{API}/conversations", json={"title": "Mine"})
    conversation_id = created.json()["id"]

    assert (
        await other_client.get(f"{API}/conversations/{conversation_id}/messages")
    ).status_code == 404
    assert (
        await other_client.get(f"{API}/conversations/{conversation_id}/turns")
    ).status_code == 404


async def test_another_learners_conversation_cannot_be_written_to(
    api_client: AsyncClient, other_client: AsyncClient
) -> None:
    created = await api_client.post(f"{API}/conversations", json={"title": "Mine"})
    conversation_id = created.json()["id"]

    posted = await other_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hello"}
    )
    assert posted.status_code == 404

    renamed = await other_client.patch(
        f"{API}/conversations/{conversation_id}", json={"title": "Now mine"}
    )
    assert renamed.status_code == 404


async def test_another_learners_conversation_cannot_be_deleted(
    api_client: AsyncClient, other_client: AsyncClient, db_session: AsyncSession
) -> None:
    created = await api_client.post(f"{API}/conversations", json={"title": "Mine"})
    conversation_id = created.json()["id"]

    assert (await other_client.delete(f"{API}/conversations/{conversation_id}")).status_code == 404
    assert await db_session.get(Conversation, uuid.UUID(conversation_id)) is not None


async def test_another_learners_conversation_cannot_be_mined_for_memories(
    api_client: AsyncClient, other_client: AsyncClient
) -> None:
    created = await api_client.post(f"{API}/conversations", json={"title": "Mine"})
    conversation_id = created.json()["id"]
    r = await other_client.post(f"{API}/conversations/{conversation_id}/memory/write-back")
    assert r.status_code == 404


# --- uploaded sources ------------------------------------------------------------------------


async def _source(session: AsyncSession, learner: Learner) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="private.pdf",
        status=SourceStatus.DONE,
        content_type="application/pdf",
        blob_key=f"b/{uuid.uuid4().hex}",
        meta={},
    )
    session.add(source)
    await session.flush()
    return source


async def test_another_learners_source_is_not_readable(
    other_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source = await _source(db_session, api_learner)
    assert (await other_client.get(f"{API}/sources/{source.id}")).status_code == 404
    assert (await other_client.get(f"{API}/sources/{source.id}/chunks")).status_code == 404
    assert (await other_client.get(f"{API}/sources/{source.id}/similar")).status_code == 404


async def test_another_learners_source_cannot_be_re_ingested(
    other_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source = await _source(db_session, api_learner)
    assert (await other_client.post(f"{API}/sources/{source.id}/retry")).status_code == 404


async def test_another_learners_source_is_not_listed(
    other_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source = await _source(db_session, api_learner)
    listed = await other_client.get(f"{API}/sources")
    assert listed.status_code == 200
    assert str(source.id) not in listed.text


# --- memory ----------------------------------------------------------------------------------


async def test_another_learners_memory_cannot_be_read_or_deleted(
    other_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    memory = Memory(
        learner_id=api_learner.id,
        kind=MemoryKind.FACT,
        content="They are revising for a September exam.",
        embedding_space=FAKE_SPACE,
        embedding=[0.0] * EMBED_DIM,
    )
    db_session.add(memory)
    await db_session.flush()

    listed = await other_client.get(f"{API}/memory")
    assert listed.status_code == 200
    assert "September exam" not in listed.text
    assert (await other_client.delete(f"{API}/memory/{memory.id}")).status_code == 404


# --- notes -----------------------------------------------------------------------------------


async def test_a_note_is_per_learner_not_per_topic(
    other_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The topic is shared curriculum; what somebody wrote about it is not."""
    _subject, topic, _kc = await _subject_topic_kc(db_session)
    db_session.add(
        Note(
            learner_id=api_learner.id,
            topic_id=topic.id,
            substrate=[
                {
                    "id": "a-1",
                    "kind": "concept",
                    "kc_ids": [],
                    "md": "My private working notes about acids.",
                    "provenance": {},
                }
            ],
        )
    )
    await db_session.flush()

    r = await other_client.get(f"{API}/topics/{topic.id}/note")
    assert "private working notes" not in r.text
    revisions = await other_client.get(f"{API}/topics/{topic.id}/note/revisions")
    assert "private working notes" not in revisions.text


# --- mastery and plans -----------------------------------------------------------------------


async def test_a_lesson_plan_is_not_another_learners_to_read(
    api_client: AsyncClient, other_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject, _topic, _kc = await _subject_topic_kc(db_session)
    await db_session.commit()

    mine = await api_client.get(f"{API}/subjects/{subject.id}/lesson-plan")
    theirs = await other_client.get(f"{API}/subjects/{subject.id}/lesson-plan")
    # Neither learner has one; the point is that asking for the subject never returns
    # somebody else's plan for it.
    assert mine.status_code == theirs.status_code == 404


async def test_mastery_is_reported_per_learner(
    other_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject, _topic, _kc = await _subject_topic_kc(db_session)
    await db_session.commit()
    r = await other_client.get(f"{API}/subjects/{subject.id}/mastery")
    assert r.status_code == 200
    # A learner with no history has assessed nothing, whoever else has practised the subject.
    assert r.json()["assessed_kcs"] == 0


# --- account data ----------------------------------------------------------------------------


async def test_an_export_contains_only_the_requesting_learner(
    other_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    db_session.add(
        Memory(
            learner_id=api_learner.id,
            kind=MemoryKind.FACT,
            content="Something only they should ever see.",
            embedding_space=FAKE_SPACE,
            embedding=[0.0] * EMBED_DIM,
        )
    )
    await db_session.flush()

    r = await other_client.get(f"{API}/me/export")
    assert r.status_code == 200
    assert "only they should ever see" not in r.text
