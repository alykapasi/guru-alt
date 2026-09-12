"""A learner can get their data out, and have all of it removed (S61).

Retention was implicit in a dozen scattered ``ondelete`` clauses, with nothing stating what
was supposed to happen — and two stores no foreign key reaches: object storage, and the items
a learner authored, whose FK is ``SET NULL`` and would have left their questions and answer
keys behind with only the author erased.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import Base
from app.llm.registry import fake_llm_client
from app.models.assessment import Item, ItemOrigin, ItemType
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.models.memory import Memory
from app.models.source import Source, SourceKind, SourceStatus
from app.services import ingestion as ingestion_svc
from app.services import memory as memory_svc
from app.services import retention as svc
from app.storage.base import BlobNotFound
from app.storage.memory import InMemoryBlobStore
from tests.embedding import FAKE_SPACE

API = "/api/v1"


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _source(session: AsyncSession, learner: Learner, *, blob_key: str | None) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.pdf",
        status=SourceStatus.PENDING,
        blob_key=blob_key,
    )
    session.add(source)
    await session.flush()
    return source


# --- the policy is executable, not prose ---------------------------------------------------


def test_every_learner_owned_table_has_a_stated_disposition() -> None:
    """The point of the map: a new learner-owned store cannot be added without someone
    choosing what deleting the account does to it."""
    named = set(svc.retention_tables())
    owned = {
        table.name
        for table in Base.metadata.tables.values()
        if any(c.name == "learner_id" for c in table.columns)
    }
    assert owned - named == set(), f"no retention decision recorded for: {owned - named}"


async def test_the_policy_is_published_not_just_documented(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/me/retention")
    assert r.status_code == 200
    stores = {s["table"]: s for s in r.json()["stores"]}
    assert stores["memories"]["disposition"] == "deleted"
    # The one intentional asymmetry, stated where a learner can read it.
    assert "conversation" in stores["memories"]["reason"]
    assert stores["llm_calls"]["disposition"] == "anonymised"


# --- export ---------------------------------------------------------------------------------


async def test_an_export_contains_the_learners_own_content(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner = api_learner
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="I study mornings.")
    )
    await _source(db_session, learner, blob_key="blobs/notes.pdf")
    await db_session.commit()

    r = await api_client.get(f"{API}/me/export")

    assert r.status_code == 200
    body = r.json()
    assert [m["content"] for m in body["messages"]] == ["I study mornings."]
    # Uploads appear as metadata; the bytes are not inlined.
    assert body["sources"][0]["blob_key"] == "blobs/notes.pdf"


async def test_an_export_leaves_out_embeddings(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Thousands of floats per row, meaningless outside the space that produced them (S50)."""
    learner = api_learner
    db_session.add(
        Memory(
            learner_id=learner.id,
            kind="fact",
            content="Studies mornings.",
            embedding=[0.1] * get_settings().embed_dim,
            embedding_space=FAKE_SPACE,
        )
    )
    await db_session.commit()

    body = (await api_client.get(f"{API}/me/export")).json()

    assert body["memories"][0]["content"] == "Studies mornings."
    assert "embedding" not in body["memories"][0]


# --- deletion --------------------------------------------------------------------------------


async def test_deletion_removes_the_uploaded_bytes_object_storage_holds(
    db_session: AsyncSession,
) -> None:
    """No foreign key reaches the blob store, so a cascade never touched it."""
    learner = await _learner(db_session)
    blobstore = InMemoryBlobStore()
    await blobstore.put("blobs/notes.pdf", b"%PDF-1.7")
    await _source(db_session, learner, blob_key="blobs/notes.pdf")
    await db_session.commit()

    report = await svc.delete_learner(db_session, blobstore, learner.id)

    assert report.blobs_deleted == 1
    assert report.complete
    with pytest.raises(BlobNotFound):
        await blobstore.get("blobs/notes.pdf")


async def test_deletion_removes_items_the_learner_authored(db_session: AsyncSession) -> None:
    """The FK is SET NULL, so a cascade would have kept the question and its answer key and
    merely forgotten who wrote them (S33)."""
    learner = await _learner(db_session)
    db_session.add(
        Item(
            item_type=ItemType.MCQ,
            stem="Mine",
            answer_key={"choices": ["a", "b"], "correct": 0},
            origin=ItemOrigin.LEARNER,
            author_learner_id=learner.id,
        )
    )
    generated = Item(item_type=ItemType.MCQ, stem="Shared", origin=ItemOrigin.GENERATED)
    db_session.add(generated)
    await db_session.commit()

    report = await svc.delete_learner(db_session, InMemoryBlobStore(), learner.id)

    assert report.items_deleted == 1
    assert await db_session.get(Item, generated.id) is not None  # the shared bank is untouched


async def test_deletion_keeps_the_spend_record_without_the_learner(
    db_session: AsyncSession,
) -> None:
    """Token spend is the platform's own accounting and has to still add up afterwards; the
    row carries no learner content."""
    learner = await _learner(db_session)
    call = LLMCall(learner_id=learner.id, role="smart", provider="fake", model="fake-1")
    db_session.add(call)
    await db_session.commit()

    await svc.delete_learner(db_session, InMemoryBlobStore(), learner.id)

    await db_session.refresh(call)
    assert call.learner_id is None
    assert call.model == "fake-1"


async def test_a_failed_blob_delete_is_reported_not_swallowed(
    db_session: AsyncSession,
) -> None:
    """The rows are already gone, so a missed key cannot be found again by walking the DB."""

    class _RefusingStore(InMemoryBlobStore):
        async def delete(self, key: str) -> None:
            raise RuntimeError("object store unavailable")

    learner = await _learner(db_session)
    await _source(db_session, learner, blob_key="blobs/notes.pdf")
    await db_session.commit()

    report = await svc.delete_learner(db_session, _RefusingStore(), learner.id)

    assert not report.complete
    assert report.blobs_failed == ["blobs/notes.pdf"]


# --- enqueued work cannot put it back --------------------------------------------------------


async def test_an_ingestion_job_queued_before_deletion_does_nothing(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner, blob_key="blobs/notes.pdf")
    source_id = source.id
    await db_session.commit()
    await svc.delete_learner(db_session, InMemoryBlobStore(), learner.id)

    result = await ingestion_svc.ingest_source(
        db_session, InMemoryBlobStore(), fake_llm_client(), source_id
    )

    assert result is None
    assert await db_session.scalar(select(func.count()).select_from(Source)) == 0


async def test_a_memory_write_back_queued_before_deletion_does_nothing(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I study."))
    conversation_id = conversation.id
    await db_session.commit()
    await svc.delete_learner(db_session, InMemoryBlobStore(), learner.id)

    written = await memory_svc.write_back(
        db_session, fake_llm_client("{}"), conversation_id=conversation_id
    )

    assert written == []
    assert await db_session.scalar(select(func.count()).select_from(Memory)) == 0
