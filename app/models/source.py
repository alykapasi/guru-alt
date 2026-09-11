"""Ingestion sources and their derived chunks (see docs/TECHNICAL_DESIGN §6, §7.1).

A ``Source`` is one uploaded file or URL; ingestion runs it through the pipeline and writes
``Chunk`` rows — each carrying the embedded text, a generated full-text vector, and
**provenance** (source id, locator, extraction method, confidence) for grounded, citable
retrieval. The raw bytes live in object storage (``blob_key``); the DB holds only metadata.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

_EMBED_DIM = get_settings().embed_dim


class SourceKind(StrEnum):
    FILE = "file"
    URL = "url"


class SourceStatus(StrEnum):
    """Ingestion lifecycle — drives idempotent, resumable jobs."""

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class Source(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One ingestible artifact (a file or a URL) and its ingestion status."""

    __tablename__ = "sources"
    __table_args__ = (
        # Reconciliation scans "what is claimable / what has been abandoned" — both are a
        # status plus a lease comparison, so they belong in one index.
        Index("ix_sources_status_lease", "status", "lease_expires_at"),
    )

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(index=True)  # SourceKind
    origin: Mapped[str]  # filename or URL
    # Object-store key for the raw bytes. Content-addressed (``blobs/<content_sha256>``), so
    # two learners uploading the same file reference one stored object. Nothing *derived* is
    # shared — each learner gets their own extraction, chunks and embeddings — and no learner
    # can observe that another references the same key.
    blob_key: Mapped[str | None] = mapped_column(default=None, index=True)
    # SHA-256 of the raw bytes. Already computed to build the key; it lives here as a column
    # because a digest buried in a path string cannot answer "do I already have this file?".
    content_sha256: Mapped[str | None] = mapped_column(default=None, index=True)
    # SHA-256 of the *canonical* extracted text (app/rag/textnorm.py) — equal for two files
    # that say the same thing in different containers or dialects, where content_sha256 shares
    # not one byte. Written after extraction, so unlike the byte hash it cannot save the cost
    # of getting there; what it saves is embedding the same book twice and then having two
    # chunks of it compete for every grounding window.
    text_sha256: Mapped[str | None] = mapped_column(default=None, index=True)
    # 64-bit SimHash of the same canonical text, as 16 hex characters. Unlike the two digests
    # above this is compared by *distance*, which is what reaches a scan: OCR errors are
    # per-character, so a photographed textbook never equals its EPUB however it is
    # normalised. Deliberately not indexed — near-neighbour search over it is a scan of the
    # learner's own sources, which is tens of rows; a cross-learner search would need LSH
    # banding, and cross-learner similarity is not something a learner may observe anyway.
    simhash: Mapped[str | None] = mapped_column(default=None)
    content_type: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(index=True, default=SourceStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Optional upload-time scope, inherited by chunks for metadata filtering.
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), index=True, default=None
    )
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), index=True, default=None
    )
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    # How many times ingestion has been *claimed* for this source. Bounded, so a source that
    # crashes its worker every time is eventually parked as FAILED instead of cycling forever.
    attempts: Mapped[int] = mapped_column(default=0)
    # When the current claim lapses. A worker that dies mid-job leaves PROCESSING behind with
    # no one working on it; the lease is what makes that state distinguishable from progress.
    lease_expires_at: Mapped[datetime | None] = mapped_column(default=None)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Chunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A retrievable text span: embedding + full-text vector + provenance."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]  # position within the source
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(_EMBED_DIM))
    # Which embedding model produced `embedding` ("provider:model:dim"). Vectors are only
    # comparable within one space, and swapping to a same-dimension model is a config edit
    # that would otherwise leave no trace — see app/llm/embedding_space.py.
    embedding_space: Mapped[str] = mapped_column(index=True)
    # Generated full-text vector for hybrid keyword retrieval (GIN-indexed above).
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)

    source: Mapped["Source"] = relationship(back_populates="chunks")
    kc_links: Mapped[list["ChunkKC"]] = relationship(
        back_populates="chunk", cascade="all, delete-orphan"
    )


class ChunkKC(UUIDPrimaryKeyMixin, Base):
    """Join row tagging a chunk to a KC it teaches, with the auto-tagger's confidence (§6, §7).

    Written during ingestion by per-chunk KC auto-tagging. Deleting a chunk cascades its tags,
    so a re-ingest (which replaces a source's chunks) naturally replaces their KC links too.
    """

    __tablename__ = "chunk_kcs"
    __table_args__ = (UniqueConstraint("chunk_id", "kc_id"),)

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), index=True
    )
    kc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    confidence: Mapped[float] = mapped_column(default=1.0)

    chunk: Mapped["Chunk"] = relationship(back_populates="kc_links")
