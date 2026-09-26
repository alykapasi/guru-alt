"""Archive, delete and forget (S61, S42; V11)."""

import json
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind
from app.models.source import Chunk, Source, SourceKind
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
