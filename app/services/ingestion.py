"""Source ingestion: store the raw bytes, then run the pipeline behind a status machine.

``create_source`` persists a ``Source`` and uploads its bytes (key =
``{learner_id}/{source_id}/{sha256}`` — learner-scoped + content-addressed). ``ingest_source``
drives PENDING → PROCESSING → DONE|FAILED, replacing the source's chunks atomically; on any
error it rolls back the partial write and records the failure.

The status machine is **claimed, not assumed** (S37). A job takes the source with one atomic
UPDATE that commits before any work starts, so PROCESSING is observable by other sessions
while it means something, and a duplicate delivery of the same job finds nothing to claim
instead of running the same extraction twice. The claim carries a lease; an expired lease is
the only evidence that distinguishes a dead worker from a slow one.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.llm import LLMClient
from app.models.source import Source, SourceKind, SourceStatus
from app.rag import pipeline
from app.rag import simhash as simhash_mod
from app.rag.demux import MediaDemuxer
from app.rag.fetch import Fetcher, FetchError, FetchTransportError, default_fetch
from app.rag.transcription import Transcriber
from app.services import knowledge
from app.storage import DEFAULT_CONTENT_TYPE, BlobStore

logger = logging.getLogger(__name__)

_HASH_CHUNK = 1024 * 1024  # 1 MiB — stream large files past the hasher without buffering them


def digest_of(data: bytes | Path) -> str:
    """SHA-256 of an upload, whether it is in memory or on disk."""
    return _digest_path(data) if isinstance(data, Path) else hashlib.sha256(data).hexdigest()


def _digest_path(path: Path) -> str:
    """SHA-256 of a file, read in chunks (bounded memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def create_source(
    session: AsyncSession,
    blobstore: BlobStore,
    *,
    learner_id: uuid.UUID,
    kind: SourceKind,
    origin: str,
    content_type: str | None,
    data: bytes | Path,
    content_sha256: str | None = None,
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
    meta: dict | None = None,
) -> Source:
    """Persist a pending source and store its blob. Caller then enqueues ingestion.

    ``data`` is either bytes (small, in-memory) or a local file ``Path`` (large uploads,
    streamed to the store without buffering). Either way the blob key is content-addressed.

    ``content_sha256`` lets a caller that has already digested the bytes — to check for a
    duplicate before getting here — avoid reading a large upload off disk a second time.
    """
    subject_id, topic_id = await knowledge.resolve_source_scope(
        session, subject_id=subject_id, topic_id=topic_id
    )
    source = Source(
        learner_id=learner_id,
        kind=kind,
        origin=origin,
        content_type=content_type,
        status=SourceStatus.PENDING,
        subject_id=subject_id,
        topic_id=topic_id,
        meta=meta or {},
    )
    session.add(source)
    await session.flush()  # assign source.id

    ctype = content_type or DEFAULT_CONTENT_TYPE
    digest = content_sha256 or digest_of(data)
    key = blob_key_for(digest)
    if isinstance(data, Path):
        await blobstore.upload(key, data, content_type=ctype)
    else:
        await blobstore.put(key, data, content_type=ctype)
    # Written unconditionally, never skipped when the key already exists: an identical write is
    # a no-op for identical bytes, and skipping would lose a race against a concurrent delete
    # of the last other reference.
    source.blob_key = key
    source.content_sha256 = digest
    try:
        await session.commit()
    except Exception:
        # The bytes are in the store but this row will not reference them. Another learner's
        # source may, though — the key is content-addressed — so this must not delete blindly.
        # Suppressed: cleaning up must never mask the error that made cleanup necessary.
        try:
            await unreference_blob(session, blobstore, key, excluding=source.id)
        except Exception:
            logger.warning("could not delete unreferenced blob %s", key, exc_info=True)
        raise
    return source


async def find_duplicate(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    content_sha256: str,
    subject_id: uuid.UUID | None,
    topic_id: uuid.UUID | None,
) -> Source | None:
    """This learner's existing source for exactly these bytes in exactly this scope.

    Scope is part of the identity on purpose. The same textbook uploaded under two subjects is
    a real intent — it covers both — and retrieval is subject-scoped, so the second copy never
    competes with the first for a place in a grounding window. Only a re-upload into the
    *same* scope is a duplicate, and that one is pure waste: a second extraction, a second set
    of embeddings, and two chunks saying the same thing crowding each other out of every
    answer.
    """
    return await session.scalar(
        select(Source)
        .where(
            Source.learner_id == learner_id,
            Source.content_sha256 == content_sha256,
            Source.subject_id.is_(subject_id)
            if subject_id is None
            else Source.subject_id == subject_id,
            Source.topic_id.is_(topic_id) if topic_id is None else Source.topic_id == topic_id,
        )
        .order_by(Source.created_at)
        .limit(1)
    )


async def create_or_reuse_source(
    session: AsyncSession,
    blobstore: BlobStore,
    *,
    learner_id: uuid.UUID,
    kind: SourceKind,
    origin: str,
    content_type: str | None,
    data: bytes | Path,
    content_sha256: str,
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
    meta: dict | None = None,
) -> tuple[Source, bool]:
    """The learner's source for these bytes, and whether it needs ingesting.

    Returns ``(source, queue_it)``. A file the learner already has in this scope is handed
    back untouched rather than ingested a second time — the saving is the whole pipeline, not
    just the storage, because this is known before a single page is OCR'd.

    One exception: a duplicate of a source that previously *failed* is put back within reach
    of a claim. Re-uploading the file is the obvious way to retry it, and refusing to would
    leave a learner re-sending a document that silently does nothing.
    """
    subject_id, topic_id = await knowledge.resolve_source_scope(
        session, subject_id=subject_id, topic_id=topic_id
    )
    existing = await find_duplicate(
        session,
        learner_id=learner_id,
        content_sha256=content_sha256,
        subject_id=subject_id,
        topic_id=topic_id,
    )
    if existing is not None:
        if existing.status != SourceStatus.FAILED:
            return existing, False
        retried = await reset_for_reingest(session, existing.id)
        return (retried or existing), retried is not None

    source = await create_source(
        session,
        blobstore,
        learner_id=learner_id,
        kind=kind,
        origin=origin,
        content_type=content_type,
        data=data,
        content_sha256=content_sha256,
        subject_id=subject_id,
        topic_id=topic_id,
        meta=meta,
    )
    return source, True


@dataclass(frozen=True)
class SimilarSource:
    """A source that looks like another, and how much of it agrees."""

    source: Source
    distance: int

    @property
    def agreement(self) -> float:
        """Share of the 64 hash bits that match — the evidence, shown rather than judged."""
        return (simhash_mod.BITS - self.distance) / simhash_mod.BITS


async def similar_sources(
    session: AsyncSession, source: Source, *, limit: int = 5
) -> list[SimilarSource]:
    """This learner's other sources, nearest first, with the distance that says why.

    A *suggestion*, never an action. ``poe simhash-separation`` measured why: a badly scanned
    copy of the same book and a document that is half this book and half another both sit
    around 16 bits apart, so no cut-off separates them. That is the limit of what shingle
    overlap can tell you rather than a threshold to tune, so the distance is reported and a
    person decides. A wrong reading then costs a wasted suggestion, not a rejected upload.

    Scanned in Python over the learner's own rows — tens of them. A cross-learner search would
    need LSH banding, for something a learner is not permitted to observe anyway.
    """
    if source.simhash is None:
        return []
    mine = simhash_mod.from_hex(source.simhash)
    others = (
        await session.scalars(
            select(Source).where(
                Source.learner_id == source.learner_id,
                Source.id != source.id,
                Source.simhash.is_not(None),
            )
        )
    ).all()
    scored = [
        SimilarSource(
            source=other, distance=simhash_mod.distance(mine, simhash_mod.from_hex(other.simhash))
        )
        for other in others
        if other.simhash is not None
    ]
    scored.sort(key=lambda s: (s.distance, s.source.created_at))
    return scored[:limit]


async def create_url_source(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    url: str,
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
) -> Source:
    """Record a pending URL source. The fetch happens in the ingestion job."""
    subject_id, topic_id = await knowledge.resolve_source_scope(
        session, subject_id=subject_id, topic_id=topic_id
    )
    source = Source(
        learner_id=learner_id,
        kind=SourceKind.URL,
        origin=url,
        content_type=None,
        status=SourceStatus.PENDING,
        subject_id=subject_id,
        topic_id=topic_id,
        meta={"url": url},
    )
    session.add(source)
    await session.commit()
    return source


def lease_seconds(settings: Settings) -> int:
    """How long a claim is honoured. Strictly longer than the job's own deadline."""
    return settings.ingest_job_timeout_seconds + settings.ingest_lease_grace_seconds


async def claim_source(
    session: AsyncSession, source_id: uuid.UUID, *, settings: Settings
) -> Source | None:
    """Atomically take ownership of one source's ingestion, committing the claim.

    Returns the claimed source, or ``None`` when there is nothing for this job to do — the
    source is already DONE, someone else holds a live lease, it has burned through
    ``ingest_max_attempts``, or the concurrency cap is full. ``None`` is not an error: a
    duplicate delivery of an already-finished job is the *expected* case, not a failure.

    Claimable means PENDING, or PROCESSING with a lapsed lease. Since the lease outlives the
    job timeout by construction, a lapsed lease can only mean the worker died — so re-claiming
    it is recovery, not a race with a running job.

    ``ingest_max_concurrent_jobs`` is enforced by a subquery inside this same UPDATE, which is
    tighter than a read-then-write but still a **soft** cap: under READ COMMITTED two claims
    racing can both see room and both take it. It bounds runaway concurrency; it is not a
    semaphore. An exact one needs advisory locks over a fixed slot set, held on a dedicated
    connection for the job's lifetime — worth doing when the cap has to be a guarantee.
    """
    lease = timedelta(seconds=lease_seconds(settings))
    live_jobs = (
        select(func.count())
        .select_from(Source)
        .where(Source.status == SourceStatus.PROCESSING, Source.lease_expires_at > func.now())
        .scalar_subquery()
    )
    claimed = await session.execute(
        update(Source)
        .where(
            Source.id == source_id,
            or_(
                Source.status == SourceStatus.PENDING,
                and_(
                    Source.status == SourceStatus.PROCESSING,
                    Source.lease_expires_at < func.now(),
                ),
            ),
            Source.attempts < settings.ingest_max_attempts,
            live_jobs < settings.ingest_max_concurrent_jobs,
        )
        .values(
            status=SourceStatus.PROCESSING,
            attempts=Source.attempts + 1,
            lease_expires_at=func.now() + lease,
            error=None,
        )
        .returning(Source.id)
    )
    if claimed.scalar_one_or_none() is None:
        # Commit, not rollback: the UPDATE matched nothing so there is no work to undo, and
        # rollback expires every object the caller has loaded — a failed claim must not
        # invalidate the session of whoever asked.
        await session.commit()
        return None
    await session.commit()  # PROCESSING is only meaningful to other sessions once committed
    return await session.get(Source, source_id, populate_existing=True)


async def dispatch(enqueue: Callable[[uuid.UUID], Awaitable[None]], source_id: uuid.UUID) -> bool:
    """Best-effort enqueue of an already-committed source. Returns whether it landed.

    A queue failure here must not fail the caller: the source row is already durable, so the
    upload genuinely succeeded and telling the learner otherwise would be a lie that also
    invites them to upload the same file again. Reconciliation collects what never dispatched.
    """
    try:
        await enqueue(source_id)
        return True
    except Exception:
        logger.warning(
            "could not enqueue ingestion for source %s; left for reconciliation",
            source_id,
            exc_info=True,
        )
        return False


@dataclass(frozen=True)
class ReconcileReport:
    """What one reconciliation sweep did. Returned so a caller can log or assert on it."""

    requeued: int
    abandoned: int


async def reconcile_stranded(
    session: AsyncSession,
    enqueue: Callable[[uuid.UUID], Awaitable[None]],
    *,
    settings: Settings,
) -> ReconcileReport:
    """Find sources nobody is working on and put them back in the queue's reach.

    A source row commits *before* its job is enqueued — they cannot be one transaction,
    because Redis is not in the database. Every scheme that pretends otherwise is really this
    one with an extra table: something durable records the intent, and something later notices
    the intent was never carried out. The ``sources`` row already is that durable record, so a
    separate outbox would add a table without adding a guarantee.

    Two ways a source strands, and both look the same from here — nothing is happening to it:
    the enqueue never landed (queue outage between commit and dispatch), or the worker holding
    it died (lapsed lease). Re-enqueueing is safe for both because the claim, not this sweep,
    decides who actually runs: a duplicate delivery finds nothing to take.

    Sources that have burned through ``ingest_max_attempts`` are parked as FAILED instead.
    Left PENDING they would be swept forever, and left invisible they would look pending to a
    learner indefinitely.
    """
    exhausted = await session.execute(
        update(Source)
        .where(
            Source.status.in_([SourceStatus.PENDING, SourceStatus.PROCESSING]),
            Source.attempts >= settings.ingest_max_attempts,
            or_(Source.lease_expires_at.is_(None), Source.lease_expires_at < func.now()),
        )
        .values(
            status=SourceStatus.FAILED,
            lease_expires_at=None,
            error=func.coalesce(
                Source.error,
                f"abandoned after {settings.ingest_max_attempts} ingestion attempts",
            ),
        )
        .returning(Source.id)
    )
    abandoned = len(exhausted.scalars().all())

    grace = timedelta(seconds=settings.ingest_reconcile_grace_seconds)
    stranded = (
        await session.scalars(
            select(Source.id)
            .where(
                Source.attempts < settings.ingest_max_attempts,
                or_(
                    and_(
                        Source.status == SourceStatus.PENDING,
                        Source.updated_at < func.now() - grace,
                    ),
                    and_(
                        Source.status == SourceStatus.PROCESSING,
                        Source.lease_expires_at < func.now(),
                    ),
                ),
            )
            .order_by(Source.updated_at)
            .limit(settings.ingest_reconcile_batch)
        )
    ).all()
    await session.commit()

    requeued = 0
    for source_id in stranded:
        try:
            await enqueue(source_id)
            requeued += 1
        except Exception:
            # The queue is still down. The row stays exactly as it is, so the next sweep
            # finds it again — that is the whole point of reconciling from durable state.
            logger.warning("could not re-enqueue stranded source %s", source_id, exc_info=True)
    if requeued or abandoned:
        logger.info("ingestion reconcile: requeued=%d abandoned=%d", requeued, abandoned)
    return ReconcileReport(requeued=requeued, abandoned=abandoned)


async def reset_for_reingest(session: AsyncSession, source_id: uuid.UUID) -> Source | None:
    """Put a finished or failed source back within the claim's reach, clearing its attempts.

    A DONE source is deliberately *not* claimable — that is what makes a duplicate job
    delivery free. Re-ingesting one is therefore an explicit act, not something a repeated
    message can cause. Refuses a source with a live claim, so this cannot yank a running job.
    """
    source = await session.get(Source, source_id, populate_existing=True)
    if source is None:
        return None
    if source.status == SourceStatus.PROCESSING and source.lease_expires_at is not None:
        live = await session.scalar(select(func.now() < source.lease_expires_at))
        if live:
            return None
    source.status = SourceStatus.PENDING
    source.attempts = 0
    source.error = None
    source.lease_expires_at = None
    await session.commit()
    return source


async def ingest_source(
    session: AsyncSession,
    blobstore: BlobStore,
    llm: LLMClient,
    source_id: uuid.UUID,
    *,
    transcriber: Transcriber | None = None,
    demuxer: MediaDemuxer | None = None,
    fetch: Fetcher = default_fetch,
    settings: Settings | None = None,
) -> Source | None:
    """Claim ``source_id`` and run the pipeline, recording DONE or FAILED.

    Returns ``None`` without doing anything if the source is not claimable (see
    ``claim_source``) — the caller must treat that as success, not as work to retry.

    For an un-fetched URL source, fetch the page (robots-aware) into the blob store first;
    a re-ingest reuses the stored bytes rather than re-hitting the URL. ``transcriber`` (ASR)
    and ``demuxer`` (video → audio track + keyframes) are used by the media path — built by the
    worker; None when no audio/video is expected.

    The whole job runs under a wall-clock deadline. Without one, a source that makes the
    pipeline pathologically slow (a huge scanned PDF, an unresponsive model) occupies a worker
    indefinitely, and no byte cap on the upload bounds that — the cost is in what the bytes
    expand into.
    """
    settings = settings or get_settings()
    source = await claim_source(session, source_id, settings=settings)
    if source is None:
        return None

    try:
        async with asyncio.timeout(settings.ingest_job_timeout_seconds):
            if source.kind == SourceKind.URL and not source.blob_key:
                await _fetch_into_blob(session, blobstore, source, fetch)
            count = await pipeline.run(
                session,
                blobstore,
                llm,
                source,
                transcriber=transcriber,
                demuxer=demuxer,
                settings=settings,
            )
    except Exception as exc:
        await session.rollback()  # discard partial chunk writes
        return await _record_failure(session, source_id, exc, settings=settings)

    source.status = SourceStatus.DONE
    source.error = None
    source.lease_expires_at = None  # done: the row is nobody's job any more
    source.meta = {**source.meta, "chunk_count": count}
    await session.commit()
    return source


async def _fetch_into_blob(
    session: AsyncSession, blobstore: BlobStore, source: Source, fetch: Fetcher
) -> None:
    """Fetch a URL source's page into the blob store, recording its content type."""
    data, content_type = await fetch(source.origin)
    digest = hashlib.sha256(data).hexdigest()
    key = blob_key_for(digest)
    await blobstore.put(key, data, content_type=content_type)
    source.blob_key = key
    source.content_sha256 = digest
    source.content_type = content_type
    await session.flush()


def blob_key_for(content_sha256: str) -> str:
    """The object key for these bytes — content alone, no learner or source in the path.

    Two learners uploading the same file therefore write the same key and one object is
    stored. Nothing derived is shared: each gets their own extraction, chunks and embeddings,
    and neither can observe that the other references it.
    """
    return f"blobs/{content_sha256}"


async def is_blob_referenced(
    session: AsyncSession, key: str, *, excluding: uuid.UUID | None = None
) -> bool:
    """Whether any source other than ``excluding`` still points at these bytes.

    Derived from ``sources`` rather than kept as a reference count. A counter drifts — every
    missed decrement leaks an object forever and every missed increment deletes one somebody
    is using — and reconciling it needs exactly this query anyway.

    ``excluding`` is what a source cleaning up after its own failed write passes: its row is
    flushed into the session, so an unqualified check sees it and concludes the bytes are in
    use by the very row that is about to be rolled back.
    """
    stmt = select(Source.id).where(Source.blob_key == key)
    if excluding is not None:
        stmt = stmt.where(Source.id != excluding)
    return (await session.scalar(stmt.limit(1))) is not None


async def unreference_blob(
    session: AsyncSession,
    blobstore: BlobStore,
    key: str,
    *,
    excluding: uuid.UUID | None = None,
) -> bool:
    """Drop a blob if nothing references it any more. ``True`` if the bytes were deleted.

    A store failure is *raised*, not swallowed: a caller deleting an account has to be able to
    report bytes it could not remove, and "still referenced" and "refused by the store" are
    opposite outcomes that must not collapse into one return value. The one caller that needs
    silence — a failed upload cleaning up after itself — suppresses it explicitly.

    The check and the delete are not atomic — the store is not in the transaction — so an
    upload that commits in the window between them leaves a row pointing at bytes just
    removed. That is a narrow race with a visible, recoverable outcome: ingestion fails on the
    missing key and ``reset_for_reingest`` puts the source back in reach after a re-upload.
    Closing it properly needs a grace period and a sweeper, which the retention policy
    deliberately does not have yet (S61).
    """
    if await is_blob_referenced(session, key, excluding=excluding):
        return False
    await blobstore.delete(key)
    return True


def _is_terminal(exc: Exception) -> bool:
    """Would running this source again produce anything but the same failure?

    Terminal failures are statements about the *source*: no adapter handles this content type,
    it extracted to nothing, it is over the per-job budget, robots forbids the URL, the URL
    resolves somewhere private. Retrying any of those is pure cost, and the learner deserves
    to be told rather than watched to spin.

    Everything else — a provider timeout, a dropped connection, a transient store error — is a
    statement about the moment and is worth another attempt. ``FetchTransportError`` is
    carved out of ``FetchError`` for exactly this reason: it is the one member of that family
    that describes the network rather than the URL.
    """
    if isinstance(exc, FetchTransportError):
        return False
    return isinstance(exc, pipeline.IngestionError | FetchError)


async def _record_failure(
    session: AsyncSession, source_id: uuid.UUID, exc: Exception, *, settings: Settings
) -> Source:
    """Record the failure, release the claim, and decide whether it is worth another go.

    A **terminal** failure lands as FAILED — a diagnosable end state a learner can be shown.
    A **transient** one goes back to PENDING, which is what makes it eligible for both a queue
    redelivery and reconciliation; the already-incremented ``attempts`` is what stops that from
    being infinite, and the last attempt is recorded as FAILED so an exhausted source is
    visible rather than parked in PENDING forever where nothing would ever look at it again.

    If *this* write also fails — a cancelled query can leave the connection unusable, which is
    exactly the case a job timeout produces — the original exception is re-raised rather than
    replaced by the bookkeeping error. The source then stays PROCESSING with a lease that
    lapses shortly after, and reconciliation picks it up: the lease is the backstop precisely
    so the failure path is allowed to fail.
    """
    try:
        source = await session.get(Source, source_id, populate_existing=True)
        if source is None:
            raise LookupError(f"source {source_id} vanished during ingestion")
        exhausted = source.attempts >= settings.ingest_max_attempts
        retryable = not _is_terminal(exc) and not exhausted
        source.status = SourceStatus.PENDING if retryable else SourceStatus.FAILED
        source.error = str(exc)[:1000]
        source.lease_expires_at = None
        await session.commit()
        return source
    except LookupError:
        raise
    except Exception:
        logger.exception("could not record ingestion failure for source %s", source_id)
        raise exc from None
