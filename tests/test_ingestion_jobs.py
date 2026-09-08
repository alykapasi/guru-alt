"""Ingestion as a *claimed* job: lease, attempt bound, concurrency cap, per-job budgets (S37).

The status machine used to be advisory — PROCESSING was flushed but never committed, so no
other session could see it, and nothing stopped two deliveries of the same job from running
the same extraction twice. These tests hold the claim to its promises. Real cross-connection
concurrency is not exercisable on the suite's savepoint-joined session (see S58); what is
exercisable, and what actually carries the guarantee, is that the claim is one atomic
statement whose second caller comes away with nothing.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Source, SourceKind, SourceStatus
from app.services import ingestion
from app.storage import InMemoryBlobStore

TEXT = b"The mitochondrion releases energy from glucose inside the cell."


async def _source(session: AsyncSession, store: InMemoryBlobStore, *, data: bytes = TEXT) -> Source:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=data,
    )


async def _run(session: AsyncSession, store: InMemoryBlobStore, source_id, **kw):
    return await ingestion.ingest_source(session, store, fake_llm_client(), source_id, **kw)


# --- the claim -------------------------------------------------------------------------


async def test_claiming_a_pending_source_marks_it_processing_with_a_lease(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    settings = Settings()

    claimed = await ingestion.claim_source(db_session, source.id, settings=settings)

    assert claimed is not None
    assert claimed.status == SourceStatus.PROCESSING
    assert claimed.attempts == 1
    assert claimed.lease_expires_at is not None


async def test_a_second_claim_while_the_lease_is_live_gets_nothing(
    db_session: AsyncSession,
) -> None:
    """A duplicate job delivery must not run the same extraction concurrently."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    settings = Settings()

    first = await ingestion.claim_source(db_session, source.id, settings=settings)
    second = await ingestion.claim_source(db_session, source.id, settings=settings)

    assert first is not None
    assert second is None


async def test_a_finished_source_is_not_claimable(db_session: AsyncSession) -> None:
    """Re-delivering a completed job costs nothing rather than re-embedding the document."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await _run(db_session, store, source.id)

    again = await _run(db_session, store, source.id)

    assert again is None
    assert source.status == SourceStatus.DONE


async def test_an_expired_lease_is_reclaimable(db_session: AsyncSession) -> None:
    """A worker that died mid-job leaves PROCESSING behind; the lapsed lease is the evidence."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.claim_source(db_session, source.id, settings=Settings())
    source.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    await db_session.commit()

    reclaimed = await ingestion.claim_source(db_session, source.id, settings=Settings())

    assert reclaimed is not None
    assert reclaimed.attempts == 2


async def test_a_source_that_has_burned_its_attempts_is_not_reclaimed(
    db_session: AsyncSession,
) -> None:
    """Otherwise a source that kills its worker every time cycles forever."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    settings = Settings(ingest_max_attempts=2)
    source.attempts = 2
    source.lease_expires_at = None
    await db_session.commit()

    assert await ingestion.claim_source(db_session, source.id, settings=settings) is None


async def test_the_concurrency_cap_refuses_a_further_claim(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    settings = Settings(ingest_max_concurrent_jobs=1)
    busy = await _source(db_session, store)
    await ingestion.claim_source(db_session, busy.id, settings=settings)
    waiting = await _source(db_session, store)

    assert await ingestion.claim_source(db_session, waiting.id, settings=settings) is None


async def test_finishing_releases_the_lease(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    done = await _run(db_session, store, source.id)

    assert done is not None
    assert done.status == SourceStatus.DONE
    assert done.lease_expires_at is None


async def test_failing_releases_the_lease(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store, data=b"   \n\t  ")  # extracts to nothing

    failed = await _run(db_session, store, source.id)

    assert failed is not None
    assert failed.status == SourceStatus.FAILED
    assert failed.lease_expires_at is None


async def test_an_unclaimable_source_does_no_pipeline_work(db_session: AsyncSession) -> None:
    """The point of returning None: the caller must not pay for the extraction again."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.claim_source(db_session, source.id, settings=Settings())

    assert await _run(db_session, store, source.id) is None
    chunks = (await db_session.scalars(select(Source).where(Source.id == source.id))).all()
    assert chunks[0].status == SourceStatus.PROCESSING  # untouched by the second caller


# --- explicit re-ingest ----------------------------------------------------------------


async def test_reset_makes_a_done_source_claimable_again(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await _run(db_session, store, source.id)

    reset = await ingestion.reset_for_reingest(db_session, source.id)

    assert reset is not None
    assert reset.status == SourceStatus.PENDING
    assert reset.attempts == 0
    assert await _run(db_session, store, source.id) is not None


async def test_reset_refuses_to_yank_a_live_claim(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.claim_source(db_session, source.id, settings=Settings())

    assert await ingestion.reset_for_reingest(db_session, source.id) is None


async def test_reset_of_a_missing_source_is_none(db_session: AsyncSession) -> None:
    assert await ingestion.reset_for_reingest(db_session, uuid.uuid4()) is None


# --- per-job budgets -------------------------------------------------------------------


async def test_a_source_over_the_character_budget_fails_before_embedding(
    db_session: AsyncSession,
) -> None:
    """The upload byte cap bounds the bytes that arrive, not what they expand into."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store, data=b"x " * 500)

    failed = await _run(
        db_session, store, source.id, settings=Settings(ingest_max_extracted_chars=10)
    )

    assert failed is not None
    assert failed.status == SourceStatus.FAILED
    assert "per-job budget" in (failed.error or "")


async def test_a_source_over_the_chunk_budget_fails_before_embedding(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    big = " ".join(f"sentence number {i} about photosynthesis." for i in range(4000)).encode()
    source = await _source(db_session, store, data=big)

    failed = await _run(db_session, store, source.id, settings=Settings(ingest_max_chunks=1))

    assert failed is not None
    assert failed.status == SourceStatus.FAILED
    assert "chunks, over the" in (failed.error or "")


async def test_the_job_deadline_releases_the_worker(db_session: AsyncSession) -> None:
    """A pathologically slow source must not occupy a worker indefinitely.

    The deadline hands the source back rather than condemning it: a timeout is usually a
    statement about the moment (a slow provider), so it is retryable — and ``attempts`` is
    what stops that from being unbounded. What the deadline guarantees is that the *worker*
    is released, which is why this asserts on the lease, not on FAILED.
    """
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    with pytest.MonkeyPatch.context() as mp:

        async def _never(*args, **kwargs):
            import asyncio

            await asyncio.sleep(3600)

        mp.setattr("app.rag.pipeline.run", _never)
        timed_out = await _run(
            db_session, store, source.id, settings=Settings(ingest_job_timeout_seconds=0)
        )

    assert timed_out is not None
    assert timed_out.status != SourceStatus.PROCESSING
    assert timed_out.lease_expires_at is None  # the worker is not holding it any more
