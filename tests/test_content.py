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

from app.api.deps import get_llm_client
from app.llm import ModelRole
from app.llm.registry import ModelSpec, fake_llm_client
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
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, api_learner: Learner
) -> None:
    learner = api_learner
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
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, api_learner: Learner
) -> None:
    learner = api_learner
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    r = await api_client.post(f"{API}/content/generate", json={"kc_id": str(kc.id)})
    assert r.status_code == 200, r.text
    assert {b["block_type"] for b in r.json()} == {"wiki_brief", "lesson"}


async def test_get_kc_content_reads_cache(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, api_learner: Learner
) -> None:
    learner = api_learner
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)
    await api_client.post(f"{API}/content/generate", json={"kc_id": str(kc.id), "type": "lesson"})

    r = await api_client.get(f"{API}/content/kc/{kc.id}")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    filtered = await api_client.get(f"{API}/content/kc/{kc.id}", params={"type": "wiki_brief"})
    assert filtered.json() == []  # only a lesson was generated


# --- cache invalidation (S29) -----------------------------------------------
#
# The old key hashed the learner, the KCs, the block type and the grounding chunk *ids*. That
# left the prompt text, the model, the KC's own wording and the chunks' text outside it — and
# the consequence of leaving a determinant out is not a missed refresh but a permanently stale
# block, served with no signal that anything changed. Each of these edits an input the old key
# could not see and asserts the block is written again.


async def _count_blocks(session: AsyncSession, kc: KC) -> int | None:
    return await session.scalar(
        select(func.count()).select_from(ContentBlock).where(ContentBlock.kc_ids.contains([kc.id]))
    )


async def test_editing_the_prompt_regenerates_rather_than_serving_the_old_wording(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this is built to stop: change how blocks are written, and every learner who
    already has one keeps reading the version written under the old instructions."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    monkeypatch.setitem(
        svc._GUIDANCE, ContentType.LESSON, "a terse lesson, no worked examples at all"
    )
    second = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert first.id != second.id
    assert await _count_blocks(db_session, kc) == 2


async def test_repointing_a_role_at_another_model_regenerates(db_session: AsyncSession) -> None:
    """A block records the model that wrote it, but recording is not invalidating: without the
    model in the key, an upgraded SMART tier keeps serving the previous model's writing."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    upgraded = _client().with_roles({ModelRole.SMART: ModelSpec(provider="fake", model="fake-2")})
    second = await svc.generate_block(
        db_session, upgraded, learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert first.model == "fake-1"
    assert second.model == "fake-2"
    assert first.id != second.id


async def test_retitling_the_kc_regenerates_even_with_the_same_grounding(
    db_session: AsyncSession,
) -> None:
    """The KC's name and description are the learning objective in the prompt. Editing them
    changes what was asked for while the grounding — all the old key could see — is untouched."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    kc.description = "Explain ATP yield per glucose molecule, with the arithmetic shown."
    await db_session.flush()
    second = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert first.id != second.id
    assert await _count_blocks(db_session, kc) == 2


async def test_a_chunk_rewritten_in_place_regenerates(db_session: AsyncSession) -> None:
    """Keying on chunk ids assumed a chunk's text never changes under a stable id. Ingestion
    replaces chunks today, so the assumption holds by accident rather than by rule — and an
    edit that keeps the id is exactly the change a key made of ids cannot see."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    a, _b = await _seed_grounding(db_session, learner)

    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    # Same chunk, same id, same embedding — only the text a prompt would carry is different.
    a.text = f"{a.text}; corrected: they also buffer calcium"
    await db_session.flush()
    second = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert first.id != second.id


async def test_an_unchanged_request_still_reuses_its_block(db_session: AsyncSession) -> None:
    """The counterweight. A key sensitive to everything is worthless if it is also unstable —
    a cache that never hits is just a slower, costlier generator, so the property that the key
    is a *function* of the request is worth pinning beside the invalidation cases."""
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)

    ids = set()
    for _ in range(3):
        block = await svc.generate_block(
            db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
        )
        ids.add(block.id)

    assert len(ids) == 1
    assert await _count_blocks(db_session, kc) == 1


async def test_a_chunk_replaced_by_an_identical_one_does_not_keep_the_old_citation(
    db_session: AsyncSession,
) -> None:
    """Re-ingestion deletes and recreates chunks, so identical text can arrive under a new id.

    The prompts are then byte-identical and a key made only of them would hit — returning a
    block whose ``citations`` name a chunk that no longer exists. The grounding ids are in the
    key for this case alone, and this is the case.
    """
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    source = await _source(db_session, learner)
    text = "mitochondria are the powerhouse of the cell"
    old_chunk = await _chunk(db_session, source, text, 0)

    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )
    assert [c["chunk_id"] for c in first.citations] == [str(old_chunk.id)]

    await db_session.delete(old_chunk)
    await db_session.flush()
    new_chunk = await _chunk(db_session, source, text, 0)  # same words, new identity

    second = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert second.id != first.id
    assert [c["chunk_id"] for c in second.citations] == [str(new_chunk.id)]
