"""The ingestion pipeline: extract → normalize → chunk → embed → store.

One pure-ish async function over a `Source`'s stored blob. The blob is **streamed to a temp
file** so even a large source never sits in memory; adapters open it from a path (PDF/EPUB
lazily). **Idempotent** — it deletes a source's existing chunks before writing new ones, so a
re-ingest replaces rather than duplicates. The caller (ingestion service / worker) owns the
transaction and the source's status; the pipeline only flushes.
"""

import asyncio
import gzip
import json
import os
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import (
    ARRAY,
    CursorResult,
    String,
    bindparam,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.learning.kc_tagging import load_candidate_kcs, tag_chunk
from app.llm import EmbedResult, LLMClient, ModelRole, Usage
from app.llm.attribution import metered
from app.llm.embedding_space import current_space
from app.llm.meter import CallRefused
from app.models.source import Chunk, ChunkKC, Source, SourceStage, SourceStatus, StagedChunk
from app.rag import extraction_quality, simhash, textnorm
from app.rag.adapters import ExtractContext, ExtractedUnit, select_adapter
from app.rag.chunking import chunk_units
from app.rag.concurrency import gather_bounded, gather_bounded_settled
from app.rag.demux import MediaDemuxer
from app.rag.transcription import Transcriber
from app.storage import BlobStore

# Bumped whenever extraction or chunking changes in a way that changes chunk text (S50).
# `poe reindex` compares every current chunk against it; see docs/RUNBOOK.md §15.
PIPELINE_VERSION = 2  # 2: structure-aware chunking (S27)

log = structlog.get_logger(__name__)


class PartialEmbedding(RuntimeError):
    """A batch failed after other batches had already been sent — and billed.

    The usage it carries is what the batches that *did* complete cost. Losing it with the
    vectors is how a retry loop spends real money and reports none of it: the source rolls
    back, the chunks are discarded, the job runs again, and the budget watch (P11) sees a
    quiet account while the provider's invoice grows with every attempt.
    """

    def __init__(self, usage: Usage, cause: BaseException) -> None:
        super().__init__(f"embedding failed after {usage.total_tokens} billed tokens: {cause}")
        self.usage = usage


async def embed_in_batches(
    llm: LLMClient, texts: Sequence[str], *, batch_size: int, concurrency: int
) -> EmbedResult:
    """Embed ``texts`` in batches of ``batch_size``, at most ``concurrency`` batches in flight.

    Returns one vector per text, **in input order**, plus the summed usage across batches —
    embedding a document is the largest single model bill in ingestion and has to be countable.
    Splitting a large document's chunks keeps each embed request bounded and lets batches
    overlap instead of one giant serial call.

    Tokens add up across batches; latency does not. The batches run concurrently, so summing
    their times would report more time than actually passed — by the concurrency factor, and
    flatteringly in the wrong direction for the one operation whose slowness anyone would be
    investigating. The elapsed time of the whole batched call is measured here instead (S48).

    A batch that fails raises :class:`PartialEmbedding` carrying what the *other* batches cost.
    The vectors are worthless without all of them and the caller will discard them — but the
    requests were made and are charged for, so the bill has to leave here even though the work
    does not.
    """
    if not texts:
        return EmbedResult(vectors=[])
    batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
    started = time.perf_counter()
    results = await gather_bounded_settled(
        [llm.embed(ModelRole.EMBED, batch) for batch in batches], concurrency
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    done = [batch for batch in results if isinstance(batch, EmbedResult)]
    usage = Usage(
        input_tokens=sum(batch.usage.input_tokens for batch in done),
        output_tokens=sum(batch.usage.output_tokens for batch in done),
        latency_ms=elapsed_ms,
    )
    failure = next((batch for batch in results if isinstance(batch, BaseException)), None)
    if failure is not None:
        # Cancellation is not a provider failure and must not be converted into one: swallowing
        # it here would break the job deadline `ingest_source` wraps this in.
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        # Nor is a refusal (S47, S49): its message is the learner's reason, and the source shows it.
        if isinstance(failure, CallRefused):
            raise failure
        raise PartialEmbedding(usage, failure) from failure
    return EmbedResult(vectors=[vector for batch in done for vector in batch.vectors], usage=usage)


class IngestionError(RuntimeError):
    """The pipeline could not produce chunks from a source."""


class UnsupportedContentType(IngestionError):
    """No adapter handles the source's content type."""


class EmptyExtraction(IngestionError):
    """Extraction yielded no usable text."""


class ExtractionTooLarge(IngestionError):
    """The source expanded past the per-job character budget."""


class TooManyChunks(IngestionError):
    """The source chunked into more pieces than one job is allowed to embed."""


async def same_text_source(session: AsyncSession, source: Source) -> Source | None:
    """Another finished source of this learner, in this scope, that says the same thing.

    Scoped the same way the byte-level check is (see ``ingestion.find_duplicate``): the same
    book under two subjects is a real intent, and retrieval is subject-scoped so the copies
    never compete. Only ``DONE`` counts — a match with no chunks behind it would leave this
    source suppressed in favour of one that cannot answer anything.

    For the same reason the match must have current chunks of its own. A twin that was itself
    suppressed as a duplicate is DONE with none, and re-extracting the original would otherwise
    defer to it — leaving the original's stale chunks in place and every reindex queueing it
    again (S50).
    """
    if source.text_sha256 is None:
        return None
    return await session.scalar(
        select(Source)
        .where(
            Source.learner_id == source.learner_id,
            Source.kind == source.kind,
            Source.text_sha256 == source.text_sha256,
            Source.status == SourceStatus.DONE,
            # An archived source answers for nothing, so it is no one's twin (S61).
            Source.archived_at.is_(None),
            Source.id != source.id,
            select(Chunk.id)
            .where(Chunk.source_id == Source.id, Chunk.superseded_at.is_(None))
            .exists(),
            Source.subject_id.is_(None)
            if source.subject_id is None
            else Source.subject_id == source.subject_id,
            Source.topic_id.is_(None)
            if source.topic_id is None
            else Source.topic_id == source.topic_id,
        )
        .order_by(Source.created_at)
        .limit(1)
    )


def artifact_key(source_id: uuid.UUID) -> str:
    """Where a source's extracted text waits between the extract and embed stages (S37)."""
    return f"ingest/{source_id}/extract.json.gz"


def _pack(units: Sequence[ExtractedUnit]) -> bytes:
    return gzip.compress(json.dumps([u.model_dump() for u in units]).encode())


def _unpack(data: bytes) -> list[ExtractedUnit]:
    return [ExtractedUnit.model_validate(u) for u in json.loads(gzip.decompress(data))]


@metered("ingestion", learner="source.learner_id")
async def run(
    session: AsyncSession,
    blobstore: BlobStore,
    llm: LLMClient,
    source: Source,
    *,
    transcriber: Transcriber | None = None,
    demuxer: MediaDemuxer | None = None,
    settings: Settings | None = None,
) -> int:
    """Ingest one source into chunks, resuming at ``source.stage`` (S37). Returns the chunk count.

    Each stage commits before the next: extract (the text is saved to the blob store), embed
    (chunks staged in committed windows), publish (one transaction makes them live and marks
    the source done). A retry, or a lapsed lease reclaimed by another worker, starts where the
    last commit left it, so OCR, transcription and embeddings are never paid for twice.

    Two budgets bound the work, both checked *before* the expensive step they guard:
    extracted characters before the text is saved, and chunk count before embedding. The
    upload byte cap does not bound either — a modest scanned PDF becomes millions of OCR'd
    characters and thousands of embed calls, and it is those that cost money and hold the
    worker.
    """
    settings = settings or get_settings()
    if source.stage is None:
        if not await _extract(session, blobstore, llm, source, transcriber, demuxer, settings):
            return 0  # a same-text duplicate: finished at extraction
    await _embed(session, blobstore, llm, source, settings)
    return await _publish(session, source)


async def _extract(
    session: AsyncSession,
    blobstore: BlobStore,
    llm: LLMClient,
    source: Source,
    transcriber: Transcriber | None,
    demuxer: MediaDemuxer | None,
    settings: Settings,
) -> bool:
    """Stage 1: extract, save the text, commit ``stage = embed``. ``False``: a duplicate, done."""
    if not source.blob_key:
        raise IngestionError("source has no stored blob")
    adapter = select_adapter(source.content_type or "")
    if adapter is None:
        raise UnsupportedContentType(f"no adapter for content type {source.content_type!r}")

    ctx = ExtractContext(
        content_type=source.content_type or "",
        origin=source.origin,
        llm=llm,
        transcriber=transcriber,
        demuxer=demuxer,
        ocr_concurrency=settings.ocr_concurrency,
    )
    fd, tmp_name = tempfile.mkstemp(dir=settings.ingest_tmp_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        await blobstore.download(source.blob_key, tmp_path)  # stream: constant memory
        units = await adapter.extract(tmp_path, meta=source.meta, ctx=ctx)
    finally:
        tmp_path.unlink(missing_ok=True)
    extracted = sum(len(unit.text) for unit in units)
    if extracted > settings.ingest_max_extracted_chars:
        raise ExtractionTooLarge(
            f"source extracted to {extracted} characters, over the "
            f"{settings.ingest_max_extracted_chars} per-job budget"
        )

    # What the source *says*, independent of the container that carried it and the dialect it
    # was written in. Recorded for every source, so later uploads have something to match.
    canonical_text = textnorm.canonical("\n".join(unit.text for unit in units))
    source.text_sha256 = textnorm.digest(canonical_text)
    source.simhash = simhash.to_hex(simhash.simhash(canonical_text))
    twin = await same_text_source(session, source)
    if twin is not None:
        # Already embedded, under this learner's own scope. Chunking it again would pay for a
        # second copy and then let the two crowd each other out of every grounding window.
        source.duplicate_of_id = twin.id
        # Its own earlier chunks go too (cited ones kept as history): a source re-ingested into
        # a scope where its twin already answers must stop answering itself, or both copies are
        # retrieved — and a stale-version original would be queued by every reindex (S77, S50).
        await supersede_chunks(session, source)
        log.info("pipeline.duplicate_text", source_id=str(source.id), duplicate_of=str(twin.id))
        _finish(source, chunk_count=0, stage=None)
        await session.commit()
        return False
    # Answering for itself from here on, so no longer anyone's duplicate (S77).
    source.duplicate_of_id = None
    await blobstore.put(artifact_key(source.id), _pack(units), content_type="application/gzip")
    source.stage = SourceStage.EMBED
    await session.commit()
    return True


async def _embed(
    session: AsyncSession, blobstore: BlobStore, llm: LLMClient, source: Source, settings: Settings
) -> None:
    """Stage 2: chunk the saved text and embed what is not staged yet, committing each window."""
    adapter = select_adapter(source.content_type or "")
    if adapter is None:
        raise UnsupportedContentType(f"no adapter for content type {source.content_type!r}")
    chunks = chunk_units(_unpack(await blobstore.get(artifact_key(source.id))))
    if not chunks:
        raise EmptyExtraction("no text extracted from source")
    if len(chunks) > settings.ingest_max_chunks:
        raise TooManyChunks(
            f"source produced {len(chunks)} chunks, over the "
            f"{settings.ingest_max_chunks} per-job budget"
        )
    staged = set(
        (
            await session.scalars(
                select(StagedChunk.ordinal).where(StagedChunk.source_id == source.id)
            )
        ).all()
    )
    todo = [(ordinal, unit) for ordinal, unit in enumerate(chunks) if ordinal not in staged]
    space = current_space(llm, dim=settings.embed_dim)
    window = settings.embed_batch_size * settings.embed_concurrency
    for start in range(0, len(todo), window):
        part = todo[start : start + window]
        # Every batch is recorded by the client as it completes, on accounting's own
        # transaction, so a partial window's charged batches survive its rollback.
        embedded = await embed_in_batches(
            llm,
            [unit.text for _, unit in part],
            batch_size=settings.embed_batch_size,
            concurrency=settings.embed_concurrency,
        )
        for (ordinal, unit), vector in zip(part, embedded.vectors, strict=True):
            session.add(
                StagedChunk(
                    source_id=source.id,
                    ordinal=ordinal,
                    text=unit.text,
                    embedding=vector,
                    embedding_space=space,
                    pipeline_version=PIPELINE_VERSION,
                    provenance=_provenance(source, unit, adapter.name),
                )
            )
        await session.commit()  # this window survives a failure in the next


def _provenance(source: Source, unit: ExtractedUnit, adapter_name: str) -> dict:
    return {
        **unit.locator,
        "source_id": str(source.id),
        "method": unit.method or adapter_name,
        # Measured indicators, where a hardcoded `"confidence": 1.0` used to sit (S27).
        # Nothing computed that number and nothing read it, and it asserted the
        # strongest possible claim — that this text is exactly what the document said
        # — about a scanned page OCR'd into nonsense just as confidently as about a
        # born-digital paragraph. These are signs of *damage*, deliberately not
        # collapsed into a score: a clean reading means nothing was detected, which is
        # not the same as the extraction being right.
        "extraction": extraction_quality.measure(unit.text).model_dump(),
    }


async def _publish(session: AsyncSession, source: Source) -> int:
    """Stage 3, one transaction: the staged chunks replace the live ones; the source is done."""
    await supersede_chunks(session, source)  # cited chunks kept as history (S29)
    columns = [
        "id",
        "source_id",
        "ordinal",
        "text",
        "embedding",
        "embedding_space",
        "pipeline_version",
        "provenance",
    ]
    staged = select(
        func.gen_random_uuid(),
        StagedChunk.source_id,
        StagedChunk.ordinal,
        StagedChunk.text,
        StagedChunk.embedding,
        StagedChunk.embedding_space,
        StagedChunk.pipeline_version,
        StagedChunk.provenance,
    ).where(StagedChunk.source_id == source.id)
    result = await session.execute(insert(Chunk).from_select(columns, staged))
    count = cast("CursorResult[Any]", result).rowcount
    await session.execute(delete(StagedChunk).where(StagedChunk.source_id == source.id))
    _finish(source, chunk_count=count, stage=SourceStage.TAG)
    await session.commit()
    return count


def _finish(source: Source, *, chunk_count: int, stage: str | None) -> None:
    """Done and nobody's job: attempts reset so a pending tag stage has its own tries."""
    source.status = SourceStatus.DONE
    source.stage = stage
    source.error = None
    source.lease_expires_at = None
    source.attempts = 0
    source.meta = {**source.meta, "chunk_count": chunk_count}


# Chunk ids a learner's own chat replies or lesson blocks cite. Scoped to the learner twice
# over: a chunk is only ever cited in its owner's material, and the scope keeps the scan to
# their rows rather than every message in the database.
_CITED = text(
    """
    SELECT c->>'chunk_id' FROM messages m
      JOIN conversations v ON v.id = m.conversation_id AND v.learner_id = :learner
      CROSS JOIN LATERAL jsonb_array_elements(m.citations) AS c
     WHERE c->>'chunk_id' = ANY(:ids)
    UNION
    SELECT c->>'chunk_id' FROM content_blocks b
      CROSS JOIN LATERAL jsonb_array_elements(b.citations) AS c
     WHERE b.learner_id = :learner AND c->>'chunk_id' = ANY(:ids)
    """
).bindparams(bindparam("ids", type_=ARRAY(String)))


async def supersede_chunks(session: AsyncSession, source: Source) -> tuple[int, int]:
    """Retire a source's current chunks before new ones are written (S29). Returns (kept, deleted).

    A chunk something cites — a chat reply or a lesson block, only ever the owner's — is kept as
    history: its text and locator stay so the citation still shows what it cited, while its
    vector and concept tags go, because nothing will search or tag it again. Everything else is
    deleted as before. Chunks superseded by an earlier re-ingest are left exactly as they are.
    """
    current = list(
        (
            await session.scalars(
                select(Chunk.id).where(Chunk.source_id == source.id, Chunk.superseded_at.is_(None))
            )
        ).all()
    )
    if not current:
        return 0, 0
    rows = await session.execute(
        _CITED, {"learner": source.learner_id, "ids": [str(i) for i in current]}
    )
    cited = {uuid.UUID(row[0]) for row in rows}
    uncited = [i for i in current if i not in cited]
    if uncited:
        await session.execute(delete(Chunk).where(Chunk.id.in_(uncited)))
    if cited:
        await session.execute(delete(ChunkKC).where(ChunkKC.chunk_id.in_(cited)))
        await session.execute(
            update(Chunk)
            .where(Chunk.id.in_(cited))
            .values(superseded_at=func.now(), embedding=None)
            .execution_options(synchronize_session=False)
        )
    return len(cited), len(uncited)


@metered("ingestion", learner="source.learner_id")
async def tag_chunks(
    session: AsyncSession,
    llm: LLMClient,
    source: Source,
    rows: Sequence[Chunk],
    settings: Settings,
) -> None:
    """Auto-tag each chunk with the KCs it teaches (scoped to the source's subject/topic).

    Best-effort enrichment: no candidate KCs ⇒ nothing to do. Tagging calls fan out with bounded
    concurrency; the resulting ``ChunkKC`` writes stay serialized on this session.
    """
    candidates = await load_candidate_kcs(session, source)
    if not candidates:
        return
    results = await gather_bounded(
        [
            tag_chunk(llm, row.text, candidates, min_confidence=settings.kc_tag_min_confidence)
            for row in rows
        ],
        settings.kc_tag_concurrency,
    )
    for row, (tags, _usage) in zip(rows, results, strict=True):
        for tag in tags:
            session.add(ChunkKC(chunk_id=row.id, kc_id=tag.kc_id, confidence=tag.confidence))
    await session.flush()


async def retag_source(
    session: AsyncSession,
    llm: LLMClient,
    source: Source,
    *,
    settings: Settings | None = None,
) -> int:
    """Rebuild one source's chunk KC tags against its *current* subject graph. Commits.

    Cheaper than a re-ingest by everything except the tagging itself: the text is already
    extracted and the embeddings are already correct, since moving a source between subjects
    changes which concepts describe it and not what it says. Returns the chunk count tagged.
    """
    settings = settings or get_settings()
    rows = list(
        (
            await session.scalars(
                select(Chunk)
                .where(Chunk.source_id == source.id, Chunk.superseded_at.is_(None))
                .order_by(Chunk.ordinal)
            )
        ).all()
    )
    if not rows:
        return 0
    await session.execute(delete(ChunkKC).where(ChunkKC.chunk_id.in_([r.id for r in rows])))
    await tag_chunks(session, llm, source, rows, settings)
    await session.commit()
    return len(rows)
