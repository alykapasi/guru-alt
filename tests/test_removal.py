"""Archive, delete and forget (S61, S42; V11)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.content import ContentBlock
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind, MemoryStatus
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import retrieval
from app.rag.scope import SourceScope
from app.services import ingestion, removal
from app.services import memory as memory_svc
from app.storage import InMemoryBlobStore
from tests.embedding import FAKE_SPACE


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _memory(
    session: AsyncSession, learner: Learner, conversation: Conversation | None, content: str
) -> Memory:
    embedding = (await fake_llm_client().embed(ModelRole.EMBED, [content])).vectors[0]
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner.id,
        conversation_id=conversation.id if conversation else None,
        origin_conversation_id=conversation.id if conversation else None,
        kind=MemoryKind.FACT,
        content=content,
        embedding=embedding,
    )
    session.add(memory)
    await session.flush()
    return memory


async def test_write_back_records_where_a_memory_came_from(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="I study in the mornings.")
    )
    await db_session.commit()
    reply = json.dumps({"memories": [{"kind": "preference", "content": "Studies in mornings"}]})

    created = await memory_svc.write_back(
        db_session, fake_llm_client(reply=reply), conversation_id=conversation.id
    )

    assert created and all(m.origin_conversation_id == conversation.id for m in created)


API = "/api/v1"
PHOTO = b"Photosynthesis converts light energy into chemical energy in chloroplasts."
PHOTO_US = b"Photosynthesis converts light energy into chemical energy in chloroplasts!"


async def _source(
    session: AsyncSession, learner: Learner, data: bytes = PHOTO, *, store=None
) -> uuid.UUID:
    store = store or InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex[:6]}.txt",
        content_type="text/plain",
        data=data,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return source.id


async def _get(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id, populate_existing=True)
    assert source is not None
    return source


async def _hits(session: AsyncSession, learner: Learner, *picked: uuid.UUID) -> list:
    scope = SourceScope(learner_id=learner.id, source_ids=tuple(picked))
    return await retrieval.retrieve(session, fake_llm_client(), "photosynthesis", scope=scope)


async def _current(session: AsyncSession, source_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(Chunk.id).where(Chunk.source_id == source_id, Chunk.superseded_at.is_(None))
    )
    return list(rows)


async def test_an_archived_source_is_never_retrieved_and_unarchive_restores_it(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    source_id = await _source(db_session, learner)
    before = await _current(db_session, source_id)
    assert await _hits(db_session, learner)

    await removal.set_source_archived(db_session, learner.id, source_id, archived=True)
    assert await _hits(db_session, learner) == []
    assert await _hits(db_session, learner, source_id) == [], "picked, still not read"

    await removal.set_source_archived(db_session, learner.id, source_id, archived=False)
    assert [h.chunk_id for h in await _hits(db_session, learner)] == before


async def test_archiving_someone_elses_source_is_not_found(db_session: AsyncSession) -> None:
    owner, stranger = await _learner(db_session), await _learner(db_session)
    source_id = await _source(db_session, owner)

    assert (
        await removal.set_source_archived(db_session, stranger.id, source_id, archived=True) is None
    )
    assert (await _get(db_session, source_id)).archived_at is None


async def test_an_archived_original_releases_its_duplicate(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    dup = await _source(db_session, learner, PHOTO_US, store=store)
    assert (await _get(db_session, dup)).duplicate_of_id == original

    await removal.set_source_archived(db_session, learner.id, original, archived=True)

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_unarchive_defers_to_a_twin_that_answers_meanwhile(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    dup = await _source(db_session, learner, PHOTO_US, store=store)
    await removal.set_source_archived(db_session, learner.id, original, archived=True)
    await ingestion.reconcile_stranded(db_session, _Queue(), settings=Settings())
    await ingestion.ingest_source(db_session, store, fake_llm_client(), dup)
    assert await _current(db_session, dup), "the duplicate now answers for itself"

    await removal.set_source_archived(db_session, learner.id, original, archived=False)

    source = await _get(db_session, original)
    assert source.duplicate_of_id == dup
    assert await _current(db_session, original) == []


async def test_unarchive_without_a_twin_simply_answers_again(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    await removal.set_source_archived(db_session, learner.id, original, archived=True)

    await removal.set_source_archived(db_session, learner.id, original, archived=False)

    assert (await _get(db_session, original)).duplicate_of_id is None
    assert await _current(db_session, original)


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def test_the_api_lists_archived_sources_apart_and_refuses_retry(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source_id = await _source(db_session, api_learner)
    await db_session.commit()

    r = await api_client.post(f"{API}/sources/{source_id}/archive")
    assert r.status_code == 200 and r.json()["archived_at"] is not None
    listed = [s["id"] for s in (await api_client.get(f"{API}/sources")).json()]
    archived = [s["id"] for s in (await api_client.get(f"{API}/sources?archived=true")).json()]
    assert str(source_id) not in listed and archived == [str(source_id)]
    r = await api_client.post(f"{API}/sources/{source_id}/retry", json={"confirm": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "archived"

    r = await api_client.post(f"{API}/sources/{source_id}/unarchive")
    assert r.status_code == 200 and r.json()["archived_at"] is None
    r = await api_client.post(f"{API}/sources/{uuid.uuid4()}/archive")
    assert r.status_code == 404


async def test_an_archived_conversation_is_listed_apart_and_read_only(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.commit()
    cid = str(conversation.id)

    r = await api_client.post(f"{API}/conversations/{cid}/archive")
    assert r.status_code == 200 and r.json()["archived_at"] is not None
    assert cid not in [c["id"] for c in (await api_client.get(f"{API}/conversations")).json()]
    archived = (await api_client.get(f"{API}/conversations?archived=true")).json()
    assert [c["id"] for c in archived] == [cid]
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "hello"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "archived"
    r = await api_client.post(f"{API}/conversations/{cid}/practice", json={"action": "skip"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "archived"
    assert (await api_client.get(f"{API}/conversations/{cid}/messages")).status_code == 200

    r = await api_client.post(f"{API}/conversations/{cid}/unarchive")
    assert r.status_code == 200 and r.json()["archived_at"] is None
    assert (await api_client.post(f"{API}/conversations/{uuid.uuid4()}/archive")).status_code == 404


async def _lesson_citing(
    session: AsyncSession, learner: Learner, source_id: uuid.UUID
) -> uuid.UUID:
    chunk_id = (await _current(session, source_id))[0]
    block = ContentBlock(
        learner_id=learner.id,
        kc_ids=[],
        block_type="lesson",
        body="Photosynthesis happens in chloroplasts.",
        citations=[{"chunk_id": str(chunk_id), "source_id": str(source_id)}],
        cache_key=f"k-{uuid.uuid4().hex}",
        model="fake-1",
    )
    session.add(block)
    await session.flush()
    return block.id


async def test_the_impact_says_what_a_delete_keeps(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source_id = await _source(db_session, learner)
    await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    impact = await removal.source_impact(db_session, learner.id, source_id)

    assert impact is not None
    assert impact.kept == {"lessons": 1, "cited_replies": 0}
    assert impact.forgettable == {"lessons": 1}
    assert any("evidence of what you can do" in note for note in impact.notes)


async def test_a_plain_delete_keeps_the_lessons_and_removes_the_file(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    source_id = await _source(db_session, learner, store=store)
    key = (await _get(db_session, source_id)).blob_key
    block_id = await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, source_id, forget=False)

    assert await db_session.get(Source, source_id) is None
    assert await db_session.get(ContentBlock, block_id) is not None
    assert key is not None and not await store.exists(key)


async def test_forget_also_removes_the_lessons_built_on_it(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    source_id = await _source(db_session, learner, store=store)
    block_id = await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, source_id, forget=True)

    assert await db_session.get(ContentBlock, block_id) is None


async def test_a_shared_file_survives_deleting_one_of_its_sources(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    first = await _source(db_session, learner, store=store)
    second = await _source(db_session, learner, store=store)  # same bytes → same blob key
    key = (await _get(db_session, first)).blob_key
    assert key == (await _get(db_session, second)).blob_key
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, first, forget=False)

    assert key is not None and await store.exists(key)


async def test_deleting_an_original_releases_its_duplicate(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    dup = await _source(db_session, learner, PHOTO_US, store=store)
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, original, forget=False)

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_a_source_mid_ingest_cannot_be_deleted(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source_id = await _source(db_session, learner)
    source = await _get(db_session, source_id)
    source.status = SourceStatus.PROCESSING
    source.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    await db_session.commit()

    with pytest.raises(removal.RemovalRefused) as refused:
        await removal.delete_source(
            db_session, InMemoryBlobStore(), learner.id, source_id, forget=False
        )
    assert refused.value.code == "ingesting"


async def test_the_api_deletes_and_says_what_it_kept(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source_id = await _source(db_session, api_learner)
    await _lesson_citing(db_session, api_learner, source_id)
    await db_session.commit()

    impact = await api_client.get(f"{API}/sources/{source_id}/removal")
    assert impact.status_code == 200 and impact.json()["kept"]["lessons"] == 1
    r = await api_client.delete(f"{API}/sources/{source_id}")
    assert r.status_code == 200 and r.json()["kept"]["lessons"] == 1
    assert (await api_client.delete(f"{API}/sources/{source_id}")).status_code == 404
    assert (await api_client.get(f"{API}/sources/{source_id}/removal")).status_code == 404


async def test_deleting_a_conversation_keeps_its_memories_and_their_origin(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.flush()
    memory = await _memory(db_session, api_learner, conversation, "Studies in the mornings")
    cid, mid = conversation.id, memory.id
    await db_session.commit()

    r = await api_client.delete(f"{API}/conversations/{cid}")

    assert r.status_code == 200 and r.json()["kept"] == {"memories": 1}
    kept = await db_session.get(Memory, mid, populate_existing=True)
    assert kept is not None and kept.status == MemoryStatus.CURRENT
    assert kept.conversation_id is None and kept.origin_conversation_id == cid
    assert (await api_client.delete(f"{API}/conversations/{cid}")).status_code == 404


async def test_forgetting_a_conversation_forgets_its_memories_for_good(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    learned = await _memory(db_session, learner, conversation, "Studies in the mornings")
    written = await _memory(db_session, learner, None, "Prefers worked examples")
    cid, learned_id, written_id = conversation.id, learned.id, written.id
    await db_session.commit()

    await removal.delete_conversation(db_session, learner.id, cid, forget=True)

    learned = await db_session.get(Memory, learned_id, populate_existing=True)
    written = await db_session.get(Memory, written_id, populate_existing=True)
    assert learned is not None and learned.status == MemoryStatus.DELETED
    assert written is not None and written.status == MemoryStatus.CURRENT, (
        "a memory the learner wrote has no origin and is never swept"
    )


async def test_forget_by_origin_works_after_the_conversation_is_gone(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = Conversation(learner_id=api_learner.id, title="Chem help")
    db_session.add(conversation)
    await db_session.flush()
    memory = await _memory(db_session, api_learner, conversation, "Studies in the mornings")
    cid, mid = conversation.id, memory.id
    await db_session.commit()

    listed = (await api_client.get(f"{API}/memory")).json()
    assert listed[0]["origin_title"] == "Chem help" and listed[0]["origin_live"] is True
    await api_client.delete(f"{API}/conversations/{cid}")
    listed = (await api_client.get(f"{API}/memory")).json()
    assert listed[0]["origin_live"] is False and listed[0]["origin_conversation_id"] == str(cid)

    r = await api_client.post(f"{API}/memory/forget-origin/{cid}")
    assert r.status_code == 200 and r.json() == {"forgotten": 1}
    again = await api_client.post(f"{API}/memory/forget-origin/{cid}")
    assert again.json() == {"forgotten": 0}
    gone = await db_session.get(Memory, mid, populate_existing=True)
    assert gone is not None and gone.status == MemoryStatus.DELETED


async def test_forgetting_clears_the_profile_watermark(db_session: AsyncSession) -> None:
    from app.models.profile import LearnerProfile

    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    db_session.add(
        LearnerProfile(
            learner_id=learner.id, evidence_watermark=datetime.now(UTC).replace(tzinfo=None)
        )
    )
    await db_session.flush()
    await _memory(db_session, learner, conversation, "Studies in the mornings")
    cid = conversation.id
    await db_session.commit()

    await removal.forget_conversation_memories(db_session, learner.id, cid)

    profile = await db_session.scalar(
        select(LearnerProfile)
        .where(LearnerProfile.learner_id == learner.id)
        .execution_options(populate_existing=True)
    )
    assert profile is not None and profile.evidence_watermark is None


async def test_forgetting_leaves_mastery_alone(db_session: AsyncSession) -> None:
    from app.models.learning import LearningEvent

    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    db_session.add(LearningEvent(learner_id=learner.id, event_type="answer", payload={}))
    await db_session.flush()
    await _memory(db_session, learner, conversation, "Studies in the mornings")
    cid = conversation.id
    await db_session.commit()

    await removal.delete_conversation(db_session, learner.id, cid, forget=True)

    count = await db_session.scalar(
        select(func.count())
        .select_from(LearningEvent)
        .where(LearningEvent.learner_id == learner.id)
    )
    assert count == 1
