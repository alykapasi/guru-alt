"""Source ingestion: store the raw bytes, then run the pipeline behind a status machine.

``create_source`` persists a ``Source`` and uploads its bytes (key =
``{learner_id}/{source_id}/{sha256}`` — learner-scoped + content-addressed). ``ingest_source``
drives PENDING → PROCESSING → DONE|FAILED, replacing the source's chunks atomically; on any
error it rolls back the partial write and records the failure.
"""

import hashlib
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from app.models.source import Source, SourceKind, SourceStatus
from app.rag import pipeline
from app.rag.fetch import Fetcher, default_fetch
from app.storage import DEFAULT_CONTENT_TYPE, BlobStore

_HASH_CHUNK = 1024 * 1024  # 1 MiB — stream large files past the hasher without buffering them


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
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
    meta: dict | None = None,
) -> Source:
    """Persist a pending source and store its blob. Caller then enqueues ingestion.

    ``data`` is either bytes (small, in-memory) or a local file ``Path`` (large uploads,
    streamed to the store without buffering). Either way the blob key is content-addressed.
    """
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
    if isinstance(data, Path):
        key = f"{learner_id}/{source.id}/{_digest_path(data)}"
        await blobstore.upload(key, data, content_type=ctype)
    else:
        key = f"{learner_id}/{source.id}/{hashlib.sha256(data).hexdigest()}"
        await blobstore.put(key, data, content_type=ctype)
    source.blob_key = key
    await session.commit()
    return source


async def create_url_source(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    url: str,
    subject_id: uuid.UUID | None = None,
    topic_id: uuid.UUID | None = None,
) -> Source:
    """Record a pending URL source. The fetch happens in the ingestion job."""
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


async def ingest_source(
    session: AsyncSession,
    blobstore: BlobStore,
    llm: LLMClient,
    source_id: uuid.UUID,
    *,
    fetch: Fetcher = default_fetch,
) -> Source:
    """Run the pipeline for ``source_id``, recording DONE or FAILED.

    For an un-fetched URL source, fetch the page (robots-aware) into the blob store first;
    a re-ingest reuses the stored bytes rather than re-hitting the URL.
    """
    source = await session.get(Source, source_id)
    if source is None:
        raise LookupError(f"source {source_id} not found")

    source.status = SourceStatus.PROCESSING
    await session.flush()
    try:
        if source.kind == SourceKind.URL and not source.blob_key:
            await _fetch_into_blob(session, blobstore, source, fetch)
        count = await pipeline.run(session, blobstore, llm, source)
    except Exception as exc:
        await session.rollback()  # discard partial chunk writes + the PROCESSING flag
        return await _mark_failed(session, source_id, exc)

    source.status = SourceStatus.DONE
    source.error = None
    source.meta = {**source.meta, "chunk_count": count}
    await session.commit()
    return source


async def _fetch_into_blob(
    session: AsyncSession, blobstore: BlobStore, source: Source, fetch: Fetcher
) -> None:
    """Fetch a URL source's page into the blob store, recording its content type."""
    data, content_type = await fetch(source.origin)
    digest = hashlib.sha256(data).hexdigest()
    key = f"{source.learner_id}/{source.id}/{digest}"
    await blobstore.put(key, data, content_type=content_type)
    source.blob_key = key
    source.content_type = content_type
    await session.flush()


async def _mark_failed(session: AsyncSession, source_id: uuid.UUID, exc: Exception) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise LookupError(f"source {source_id} vanished during ingestion")
    source.status = SourceStatus.FAILED
    source.error = str(exc)[:1000]
    await session.commit()
    return source
