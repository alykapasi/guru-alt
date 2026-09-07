"""Content engine: grounded + cached block generation, assembly, and the endpoints.

The FakeProvider returns a fixed reply, so we hand it a JSON block ``{"body", "citations"}``
to simulate a well-behaved model. Grounding chunks are seeded with text matching the KC so
hybrid retrieval returns them; cited indices then resolve to those real chunk ids.
"""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE, get_llm_client
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.content import ContentBlock, ContentType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.services import content as svc
from tests.embedding import FAKE_SPACE

API = "/api/v1"

# A reply the FakeProvider can return verbatim — a valid block citing the first two snippets.
_BLOCK_REPLY = json.dumps(
    {"body": "Mitochondria generate ATP via cellular respiration.", "citations": [0, 1]}
)


def _client(reply: str = _BLOCK_REPLY):
    return fake_llm_client(reply=reply)


async def _embed(text: str) -> list[float]:
    return (await _client().embed(ModelRole.EMBED, [text])).vectors[0]


async def _learner(session: AsyncSession, *, handle: str | None = None) -> Learner:
    learner = Learner(handle=handle or f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kc(session: AsyncSession, name: str = "Mitochondria") -> KC:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="Cells")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"kc-{uuid.uuid4().hex[:8]}", name=name)
    session.add(kc)
    await session.flush()
    return kc


async def _chunk(session: AsyncSession, source: Source, text: str, ordinal: int = 0) -> Chunk:
    chunk = Chunk(
        embedding_space=FAKE_SPACE,
        source_id=source.id,
        ordinal=ordinal,
        text=text,
        embedding=await _embed(text),
        provenance={"source_id": str(source.id), "method": "text"},
    )
    session.add(chunk)
    await session.flush()
    return chunk


async def _source(session: AsyncSession, learner: Learner) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="bio.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        meta={},
    )
    session.add(source)
    await session.flush()
    return source


async def _seed_grounding(session: AsyncSession, learner: Learner) -> tuple[Chunk, Chunk]:
    source = await _source(session, learner)
    a = await _chunk(session, source, "mitochondria are the powerhouse of the cell", 0)
    b = await _chunk(session, source, "mitochondria produce ATP through respiration", 1)
    return a, b


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: _client()
    yield
    app.dependency_overrides.pop(get_llm_client, None)


# --- generation -------------------------------------------------------------


async def test_generate_block_grounded_and_cached(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    a, b = await _seed_grounding(db_session, learner)

    block = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert block.kc_ids == [kc.id]
    assert block.block_type == ContentType.LESSON
    assert "Mitochondria" in block.body
    cited = {c["chunk_id"] for c in block.citations}
    assert cited  # the block is grounded
    assert cited <= {str(a.id), str(b.id)}  # every citation is a real seeded chunk


async def test_generate_block_reuses_cache(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )
    second = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert first.id == second.id  # reused, not regenerated
    count = await db_session.scalar(
        select(func.count()).select_from(ContentBlock).where(ContentBlock.kc_ids.contains([kc.id]))
    )
    assert count == 1


async def test_assemble_returns_default_block_set(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    blocks = await svc.assemble(db_session, _client(), learner_id=learner.id, kc_id=kc.id)

    assert [b.block_type for b in blocks] == list(svc.DEFAULT_ASSEMBLY)
    assert len({b.id for b in blocks}) == 2  # distinct blocks per type


async def test_generate_block_unknown_kc_raises(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    with pytest.raises(LookupError):
        await svc.generate_block(
            db_session,
            _client(),
            learner_id=learner.id,
            kc_id=uuid.uuid4(),
            block_type=ContentType.LESSON,
        )


async def test_list_blocks_scoped_to_learner(db_session: AsyncSession) -> None:
    l1, l2 = await _learner(db_session), await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, l1)
    mine = await svc.generate_block(
        db_session, _client(), learner_id=l1.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert [b.id for b in await svc.list_blocks(db_session, learner_id=l1.id, kc_id=kc.id)] == [
        mine.id
    ]
    assert await svc.list_blocks(db_session, learner_id=l2.id, kc_id=kc.id) == []


# --- endpoints --------------------------------------------------------------


async def test_generate_endpoint_with_type(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = await _learner(db_session, handle=DEV_LEARNER_HANDLE)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    r = await api_client.post(
        f"{API}/content/generate", json={"kc_id": str(kc.id), "type": "lesson"}
    )
    assert r.status_code == 200, r.text
    blocks = r.json()
    assert len(blocks) == 1
    assert blocks[0]["block_type"] == "lesson"
    assert blocks[0]["citations"]


async def test_generate_endpoint_assembles_without_type(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = await _learner(db_session, handle=DEV_LEARNER_HANDLE)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    r = await api_client.post(f"{API}/content/generate", json={"kc_id": str(kc.id)})
    assert r.status_code == 200, r.text
    assert {b["block_type"] for b in r.json()} == {"wiki_brief", "lesson"}


async def test_get_kc_content_reads_cache(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = await _learner(db_session, handle=DEV_LEARNER_HANDLE)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)
    await api_client.post(f"{API}/content/generate", json={"kc_id": str(kc.id), "type": "lesson"})

    r = await api_client.get(f"{API}/content/kc/{kc.id}")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    filtered = await api_client.get(f"{API}/content/kc/{kc.id}", params={"type": "wiki_brief"})
    assert filtered.json() == []  # only a lesson was generated
