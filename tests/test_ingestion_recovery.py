"""Durable dispatch and recovery for ingestion (S36).

A source row commits before its job is enqueued — they cannot be one transaction, because
Redis is not in the database. So the interesting cases are all the ones between those two
commits, and after: the queue is down when the upload lands, the worker dies mid-job, the
failure is worth retrying, the failure is not.
"""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE
from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Source, SourceKind, SourceStatus
from app.rag.fetch import FetchError, FetchTransportError, RobotsDisallowed
from app.rag.pipeline import IngestionError
from app.services import ingestion
from app.storage import InMemoryBlobStore

TEXT = b"Photosynthesis converts light energy into chemical energy."


class _DeadQueue:
    """A queue that is down. Every dispatch raises."""

    async def __call__(self, source_id: uuid.UUID) -> None:
        raise ConnectionError("redis is down")


class _RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def _source(
    session: AsyncSession, store: InMemoryBlobStore, *, data: bytes = TEXT, dev: bool = False
) -> Source:
    """``dev=True`` owns the source by the learner the stubbed auth resolves to, so an API
    call in the same test can actually see it."""
    learner = Learner(handle=DEV_LEARNER_HANDLE if dev else f"l-{uuid.uuid4().hex[:8]}")
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


def _ago(**kw) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(**kw)


# --- dispatch survives a dead queue ------------------------------------------------------


async def test_dispatch_reports_failure_instead_of_raising(db_session: AsyncSession) -> None:
    """The row is already durable, so the upload really did succeed."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    assert await ingestion.dispatch(_DeadQueue(), source.id) is False
    assert source.status == SourceStatus.PENDING  # still there, still to do


async def test_upload_still_succeeds_when_the_queue_is_down(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.api.deps import get_blob_store, get_ingestion_enqueuer
    from app.main import app

    # The object store has to be overridden too: without it this reaches the real S3/MinIO
    # endpoint, which passes on a laptop running docker compose and fails in CI.
    app.dependency_overrides[get_blob_store] = lambda: InMemoryBlobStore()
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: _DeadQueue()
    try:
        response = await api_client.post(
            "/api/v1/sources/upload", files={"file": ("n.txt", TEXT, "text/plain")}
        )
    finally:
        app.dependency_overrides.pop(get_ingestion_enqueuer, None)
        app.dependency_overrides.pop(get_blob_store, None)

    assert response.status_code == 202
    assert response.json()["status"] == SourceStatus.PENDING


# --- reconciliation ----------------------------------------------------------------------


async def test_a_source_stranded_by_a_dead_queue_is_requeued(db_session: AsyncSession) -> None:
    """The second-pass check: simulate a queue failure right after the upload commits."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.dispatch(_DeadQueue(), source.id)
    source.updated_at = _ago(minutes=30)
    await db_session.commit()
    queue = _RecordingQueue()

    report = await ingestion.reconcile_stranded(db_session, queue, settings=Settings())

    assert report.requeued == 1
    assert queue.enqueued == [source.id]


async def test_a_freshly_uploaded_source_is_left_alone(db_session: AsyncSession) -> None:
    """A source merely waiting its turn in the queue is not stranded."""
    store = InMemoryBlobStore()
    await _source(db_session, store)
    queue = _RecordingQueue()

    report = await ingestion.reconcile_stranded(db_session, queue, settings=Settings())

    assert report.requeued == 0
    assert queue.enqueued == []


async def test_a_job_whose_worker_died_is_requeued(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.claim_source(db_session, source.id, settings=Settings())
    source.lease_expires_at = _ago(minutes=5)  # the worker never came back
    await db_session.commit()
    queue = _RecordingQueue()

    report = await ingestion.reconcile_stranded(db_session, queue, settings=Settings())

    assert report.requeued == 1
    assert queue.enqueued == [source.id]


async def test_a_live_job_is_not_requeued(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.claim_source(db_session, source.id, settings=Settings())
    queue = _RecordingQueue()

    report = await ingestion.reconcile_stranded(db_session, queue, settings=Settings())

    assert report.requeued == 0


async def test_an_exhausted_source_is_parked_as_failed_not_swept_forever(
    db_session: AsyncSession,
) -> None:
    """Left PENDING it would be swept every tick; left invisible it would look pending."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    source.attempts = 3
    source.updated_at = _ago(minutes=30)
    await db_session.commit()
    queue = _RecordingQueue()

    report = await ingestion.reconcile_stranded(
        db_session, queue, settings=Settings(ingest_max_attempts=3)
    )

    assert report.abandoned == 1
    assert report.requeued == 0
    await db_session.refresh(source)
    assert source.status == SourceStatus.FAILED
    assert "abandoned after 3" in (source.error or "")


async def test_parking_keeps_the_last_real_error(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    source.attempts = 3
    source.error = "provider timed out"
    source.updated_at = _ago(minutes=30)
    await db_session.commit()

    await ingestion.reconcile_stranded(
        db_session, _RecordingQueue(), settings=Settings(ingest_max_attempts=3)
    )

    await db_session.refresh(source)
    assert source.error == "provider timed out"  # more useful than a generic give-up message


async def test_a_queue_still_down_leaves_the_source_for_the_next_sweep(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    source.updated_at = _ago(minutes=30)
    await db_session.commit()

    report = await ingestion.reconcile_stranded(db_session, _DeadQueue(), settings=Settings())

    assert report.requeued == 0
    await db_session.refresh(source)
    assert source.status == SourceStatus.PENDING  # untouched, so the next sweep finds it


# --- retry classification ----------------------------------------------------------------


async def test_a_terminal_failure_lands_as_failed(db_session: AsyncSession) -> None:
    """Nothing about running an unsupported content type again would go differently."""
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="x.bin",
        content_type="application/x-unknown",
        data=b"\x00\x01",
    )

    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert result is not None
    assert result.status == SourceStatus.FAILED


async def test_a_transient_failure_goes_back_to_pending(db_session: AsyncSession) -> None:
    """A provider blip is a statement about the moment, not about the source."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    import pytest

    with pytest.MonkeyPatch.context() as mp:

        async def _blip(*args, **kwargs):
            raise ConnectionError("provider unreachable")

        mp.setattr("app.rag.pipeline.run", _blip)
        result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert result is not None
    assert result.status == SourceStatus.PENDING
    assert result.attempts == 1
    assert "unreachable" in (result.error or "")


async def test_a_transient_failure_on_the_last_attempt_is_final(db_session: AsyncSession) -> None:
    """PENDING with no attempts left would be a source nothing ever looks at again."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    import pytest

    with pytest.MonkeyPatch.context() as mp:

        async def _blip(*args, **kwargs):
            raise ConnectionError("provider unreachable")

        mp.setattr("app.rag.pipeline.run", _blip)
        result = await ingestion.ingest_source(
            db_session,
            store,
            fake_llm_client(),
            source.id,
            settings=Settings(ingest_max_attempts=1),
        )

    assert result is not None
    assert result.status == SourceStatus.FAILED


def test_facts_about_the_source_are_terminal_and_facts_about_the_moment_are_not() -> None:
    """The distinction the whole retry policy rests on."""
    assert ingestion._is_terminal(IngestionError("no adapter for this content type"))
    assert ingestion._is_terminal(RobotsDisallowed("robots.txt disallows this URL"))
    assert ingestion._is_terminal(FetchError("resolves to a non-public address"))

    assert not ingestion._is_terminal(FetchTransportError("connection reset"))
    assert not ingestion._is_terminal(ConnectionError("provider unreachable"))


async def test_a_robots_block_is_not_retried(db_session: AsyncSession) -> None:
    """It is a permanent fact about the URL; retrying re-asks a question already answered."""
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    source = await ingestion.create_url_source(
        db_session, learner_id=learner.id, url="https://example.com/x"
    )

    async def _blocked(url: str):
        raise RobotsDisallowed(f"robots.txt disallows {url}")

    result = await ingestion.ingest_source(
        db_session, store, fake_llm_client(), source.id, fetch=_blocked
    )

    assert result is not None
    assert result.status == SourceStatus.FAILED  # not PENDING: nothing would change


# --- orphaned blobs ----------------------------------------------------------------------


async def test_a_failed_commit_does_not_leave_the_blob_behind(db_session: AsyncSession) -> None:
    """Nothing would ever reference that key again, so it would sit there billed forever."""
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    import pytest

    with pytest.MonkeyPatch.context() as mp:

        async def _boom(*args, **kwargs):
            raise RuntimeError("commit failed")

        mp.setattr(type(db_session), "commit", _boom)
        with pytest.raises(RuntimeError):
            await ingestion.create_source(
                db_session,
                store,
                learner_id=learner.id,
                kind=SourceKind.FILE,
                origin="notes.txt",
                content_type="text/plain",
                data=TEXT,
            )

    assert store._store == {}  # nothing orphaned


# --- the retry surface -------------------------------------------------------------------


async def test_retry_endpoint_requeues_a_finished_source(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store, dev=True)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    response = await api_client.post(f"/api/v1/sources/{source.id}/retry")

    assert response.status_code == 202
    assert response.json()["status"] == SourceStatus.PENDING


async def test_retry_refuses_while_a_job_holds_the_claim(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store, dev=True)
    await ingestion.claim_source(db_session, source.id, settings=Settings())

    response = await api_client.post(f"/api/v1/sources/{source.id}/retry")

    assert response.status_code == 409


async def test_retry_of_another_learners_source_is_404(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)  # owned by a fresh learner, not the caller

    response = await api_client.post(f"/api/v1/sources/{source.id}/retry")

    assert response.status_code == 404
