"""Source/Chunk persistence: pgvector cosine search + generated full-text vector."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from tests.embedding import FAKE_SPACE

_DIM = 768


def _vec(*head: float) -> list[float]:
    """A 768-dim vector with the given leading components, zero-padded."""
    return list(head) + [0.0] * (_DIM - len(head))


async def _seed_source(session: AsyncSession) -> Source:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        blob_key=f"{learner.id}/x/abc",
        content_type="text/plain",
        status=SourceStatus.DONE,
    )
    session.add(source)
    await session.flush()
    return source


async def test_source_and_chunk_roundtrip(db_session: AsyncSession) -> None:
    source = await _seed_source(db_session)
    chunk = Chunk(
        embedding_space=FAKE_SPACE,
        source_id=source.id,
        ordinal=0,
        text="The mitochondria is the powerhouse of the cell.",
        embedding=_vec(1.0),
        provenance={
            "source_id": str(source.id),
            "locator": "p1",
            "method": "text",
            "confidence": 1.0,
        },
    )
    db_session.add(chunk)
    await db_session.flush()

    got = await db_session.scalar(select(Chunk).where(Chunk.id == chunk.id))
    assert got is not None
    assert got.provenance["method"] == "text"
    assert got.source_id == source.id


async def test_cosine_search_returns_nearest(db_session: AsyncSession) -> None:
    source = await _seed_source(db_session)
    near = Chunk(
        embedding_space=FAKE_SPACE,
        source_id=source.id,
        ordinal=0,
        text="alpha",
        embedding=_vec(1.0, 0.0),
    )
    far = Chunk(
        embedding_space=FAKE_SPACE,
        source_id=source.id,
        ordinal=1,
        text="beta",
        embedding=_vec(0.0, 1.0),
    )
    db_session.add_all([near, far])
    await db_session.flush()

    query = _vec(0.9, 0.1)
    nearest = await db_session.scalar(
        select(Chunk.id).order_by(Chunk.embedding.cosine_distance(query)).limit(1)
    )
    assert nearest == near.id


async def test_generated_tsv_supports_full_text_search(db_session: AsyncSession) -> None:
    source = await _seed_source(db_session)
    db_session.add_all(
        [
            Chunk(
                embedding_space=FAKE_SPACE,
                source_id=source.id,
                ordinal=0,
                text="Photosynthesis converts light to energy.",
                embedding=_vec(1.0),
            ),
            Chunk(
                embedding_space=FAKE_SPACE,
                source_id=source.id,
                ordinal=1,
                text="Newton described the laws of motion.",
                embedding=_vec(0.5),
            ),
        ]
    )
    await db_session.flush()

    hits = (
        await db_session.scalars(
            select(Chunk.text).where(
                Chunk.tsv.op("@@")(func.plainto_tsquery("english", "photosynthesis"))
            )
        )
    ).all()
    assert hits == ["Photosynthesis converts light to energy."]


async def test_deleting_source_cascades_to_chunks(db_session: AsyncSession) -> None:
    source = await _seed_source(db_session)
    db_session.add(
        Chunk(
            embedding_space=FAKE_SPACE,
            source_id=source.id,
            ordinal=0,
            text="x",
            embedding=_vec(1.0),
        )
    )
    await db_session.flush()

    await db_session.delete(source)
    await db_session.flush()
    remaining = (await db_session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    assert remaining == []
