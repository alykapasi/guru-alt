"""Per-learner memory: durable facts/preferences/summaries extracted from conversations
(see docs/MASTERPLAN §5, docs/TECHNICAL_DESIGN §7.1, §9).

Distinct from the learner profile (``app/models/profile.py``): the profile models *how* a
learner learns (behavior-derived, numeric/enum dimensions); memory holds discrete *facts about*
them in natural language, retrieved by embedding similarity and folded into tutor-turn context
(``app.memory.retrieval``). Vector-only retrieval — no full-text (``tsvector``) column, unlike
``Chunk`` — the smaller, short-text nature of memory content doesn't need hybrid keyword fusion
for v1; add one later if semantic-only retrieval proves insufficient.

Deliberately no ``source`` column (contrast ``ProfileDimension.source``): every row today comes
from conversation extraction, so ``conversation_id``'s nullability already carries that signal;
add a real ``source`` column if/when a second origin exists, rather than now (YAGNI).

Rows are **soft-deleted and superseded, never removed** (S42). ``status`` carries the whole
lifecycle, and the reason is the embedding: a deleted row's vector is the only thing that can
recognise the same fact being extracted again, so hard-deleting the row is what allowed a
learner to delete a memory and have it reappear from the same history. A superseded row is
kept for the same reason and one more — it is the record that the learner corrected something,
which a bare overwrite would discard.
"""

import uuid
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

_EMBED_DIM = get_settings().embed_dim


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    SUMMARY = "summary"


class MemoryStatus(StrEnum):
    """Where a memory stands. Only CURRENT is ever retrieved.

    SUPERSEDED and DELETED rows stay in the table because their embeddings still do work:
    they are what lets extraction recognise "this is that fact again" — as a correction to
    apply, or as something the learner has already said they do not want kept.
    """

    CURRENT = "current"
    SUPERSEDED = "superseded"  # the learner said something newer that contradicts it
    DELETED = "deleted"  # the learner removed it; it must not come back


class Memory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One durable, embedded fact/preference/summary about a learner."""

    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    # Provenance — the conversation this was extracted from. Nullable/SET NULL: a memory
    # outlives the conversation it came from (unlike a chat message).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True, default=None
    )
    kind: Mapped[str] = mapped_column(index=True)  # MemoryKind
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(_EMBED_DIM))
    # Which embedding model produced `embedding` ("provider:model:dim"). Vectors are only
    # comparable within one space, and swapping to a same-dimension model is a config edit
    # that would otherwise leave no trace — see app/llm/embedding_space.py.
    embedding_space: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(index=True, default=MemoryStatus.CURRENT)
    # The memory that replaced this one, when a later extraction contradicted it. Keeping the
    # chain rather than overwriting means a correction is visible as a correction.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL"), default=None
    )
