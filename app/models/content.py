"""Generated content blocks: KC-tagged, grounded, cached teaching material.

A ``ContentBlock`` is a unit of generated instruction (a lesson, a wiki entry, a question)
**tagged to one or more KCs** and **grounded in retrieved chunks** (its ``citations``). Blocks
are content-addressed by ``cache_key`` — a hash over the learner, KCs, type, and the grounding
set — so identical requests reuse a block instead of regenerating it. When the grounding
changes (new sources ingested), the key changes and a fresh block is generated.
"""

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ContentType(StrEnum):
    """The kind of generated block; also selects the model tier that produces it."""

    LESSON = "lesson"  # a teach-it explanation with worked detail
    WIKI_BRIEF = "wiki_brief"  # a few-sentence reference summary
    WIKI_FULL = "wiki_full"  # a thorough reference article (synthesis → GENIUS)
    QUESTION = "question"  # a practice prompt


class ContentBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A cached, KC-tagged block of generated content grounded in cited chunks."""

    __tablename__ = "content_blocks"
    __table_args__ = (Index("ix_content_blocks_kc_ids", "kc_ids", postgresql_using="gin"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    kc_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))
    block_type: Mapped[str] = mapped_column(index=True)  # ContentType
    body: Mapped[str] = mapped_column(Text)
    # Each citation: {"chunk_id": str, "source_id": str} — points at real retrieved chunks.
    citations: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    # Content-addressed dedup key over (learner, kc_ids, type, grounding-set).
    cache_key: Mapped[str] = mapped_column(unique=True, index=True)
    model: Mapped[str]  # the resolved model that produced this block
