"""The ingestion pipeline: extract → normalize → chunk → embed → store.

One pure-ish async function over a `Source`'s stored bytes. **Idempotent** — it deletes a
source's existing chunks before writing new ones, so a re-ingest replaces rather than
duplicates. The caller (ingestion service / worker) owns the transaction and the source's
status; the pipeline only flushes.
"""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient, ModelRole
from app.models.source import Chunk, Source
from app.rag.adapters import select_adapter
from app.rag.chunking import chunk_units
from app.storage import BlobStore


class IngestionError(RuntimeError):
    """The pipeline could not produce chunks from a source."""


class UnsupportedContentType(IngestionError):
    """No adapter handles the source's content type."""


class EmptyExtraction(IngestionError):
    """Extraction yielded no usable text."""


async def run(session: AsyncSession, blobstore: BlobStore, llm: LLMClient, source: Source) -> int:
    """Ingest one source into chunks. Returns the chunk count. Flushes; caller commits."""
    if not source.blob_key:
        raise IngestionError("source has no stored blob")
    adapter = select_adapter(source.content_type or "")
    if adapter is None:
        raise UnsupportedContentType(f"no adapter for content type {source.content_type!r}")

    data = await blobstore.get(source.blob_key)
    chunks = chunk_units(adapter.extract(data, meta=source.meta))
    if not chunks:
        raise EmptyExtraction("no text extracted from source")

    vectors = await llm.embed(ModelRole.EMBED, [c.text for c in chunks])

    # Idempotent: replace any prior chunks for this source.
    await session.execute(delete(Chunk).where(Chunk.source_id == source.id))
    for ordinal, (unit, vector) in enumerate(zip(chunks, vectors, strict=True)):
        session.add(
            Chunk(
                source_id=source.id,
                ordinal=ordinal,
                text=unit.text,
                embedding=vector,
                provenance={
                    **unit.locator,
                    "source_id": str(source.id),
                    "method": adapter.name,
                    "confidence": 1.0,
                },
            )
        )
    await session.flush()
    return len(chunks)
