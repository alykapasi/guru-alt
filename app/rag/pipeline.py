"""The ingestion pipeline: extract → normalize → chunk → embed → store.

One pure-ish async function over a `Source`'s stored blob. The blob is **streamed to a temp
file** so even a large source never sits in memory; adapters open it from a path (PDF/EPUB
lazily). **Idempotent** — it deletes a source's existing chunks before writing new ones, so a
re-ingest replaces rather than duplicates. The caller (ingestion service / worker) owns the
transaction and the source's status; the pipeline only flushes.
"""

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.learning.kc_tagging import TAGGING_ROLE, load_candidate_kcs, tag_chunk
from app.llm import EmbedResult, LLMClient, ModelRole, Usage
from app.models.source import Chunk, ChunkKC, Source
from app.rag.adapters import ExtractContext, select_adapter
from app.rag.chunking import chunk_units
from app.rag.concurrency import gather_bounded
from app.rag.demux import MediaDemuxer
from app.rag.transcription import Transcriber
from app.services.llm_log import log_llm_call
from app.storage import BlobStore


async def embed_in_batches(
    llm: LLMClient, texts: Sequence[str], *, batch_size: int, concurrency: int
) -> EmbedResult:
    """Embed ``texts`` in batches of ``batch_size``, at most ``concurrency`` batches in flight.

    Returns one vector per text, **in input order**, plus the summed usage across batches —
    embedding a document is the largest single model bill in ingestion and has to be countable.
    Splitting a large document's chunks keeps each embed request bounded and lets batches
    overlap instead of one giant serial call.
    """
    if not texts:
        return EmbedResult(vectors=[])
    batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
    results = await gather_bounded(
        [llm.embed(ModelRole.EMBED, batch) for batch in batches], concurrency
    )
    return EmbedResult(
        vectors=[vector for batch in results for vector in batch.vectors],
        usage=Usage(
            input_tokens=sum(batch.usage.input_tokens for batch in results),
            output_tokens=sum(batch.usage.output_tokens for batch in results),
        ),
    )


class IngestionError(RuntimeError):
    """The pipeline could not produce chunks from a source."""


class UnsupportedContentType(IngestionError):
    """No adapter handles the source's content type."""


class EmptyExtraction(IngestionError):
    """Extraction yielded no usable text."""


async def run(
    session: AsyncSession,
    blobstore: BlobStore,
    llm: LLMClient,
    source: Source,
    *,
    transcriber: Transcriber | None = None,
    demuxer: MediaDemuxer | None = None,
) -> int:
    """Ingest one source into chunks. Returns the chunk count. Flushes; caller commits."""
    if not source.blob_key:
        raise IngestionError("source has no stored blob")
    adapter = select_adapter(source.content_type or "")
    if adapter is None:
        raise UnsupportedContentType(f"no adapter for content type {source.content_type!r}")

    settings = get_settings()
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
    chunks = chunk_units(units)
    if not chunks:
        raise EmptyExtraction("no text extracted from source")

    # Log any model calls extraction made (vision-OCR).
    for role, usage in ctx.usage_log:
        await log_llm_call(
            learner_id=source.learner_id, role=str(role), spec=llm.spec(role), usage=usage
        )

    embedded = await embed_in_batches(
        llm,
        [c.text for c in chunks],
        batch_size=settings.embed_batch_size,
        concurrency=settings.embed_concurrency,
    )
    if embedded.usage.total_tokens:
        await log_llm_call(
            learner_id=source.learner_id,
            role=str(ModelRole.EMBED),
            spec=llm.spec(ModelRole.EMBED),
            usage=embedded.usage,
        )

    # Idempotent: replace any prior chunks for this source (their KC tags cascade away with them).
    await session.execute(delete(Chunk).where(Chunk.source_id == source.id))
    rows: list[Chunk] = []
    for ordinal, (unit, vector) in enumerate(zip(chunks, embedded.vectors, strict=True)):
        row = Chunk(
            source_id=source.id,
            ordinal=ordinal,
            text=unit.text,
            embedding=vector,
            provenance={
                **unit.locator,
                "source_id": str(source.id),
                "method": unit.method or adapter.name,
                "confidence": 1.0,
            },
        )
        session.add(row)
        rows.append(row)
    await session.flush()  # assign chunk ids so KC tags can reference them

    await _tag_chunks(session, llm, source, rows, settings)
    return len(chunks)


async def _tag_chunks(
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
    for row, (tags, usage) in zip(rows, results, strict=True):
        for tag in tags:
            session.add(ChunkKC(chunk_id=row.id, kc_id=tag.kc_id, confidence=tag.confidence))
        if usage.input_tokens or usage.output_tokens:
            await log_llm_call(
                learner_id=source.learner_id,
                role=str(TAGGING_ROLE),
                spec=llm.spec(TAGGING_ROLE),
                usage=usage,
            )
    await session.flush()
