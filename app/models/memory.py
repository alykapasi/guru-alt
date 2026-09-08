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

Deletion (``DELETE /memory/{id}`` and the bulk ``DELETE /memory``) has no tombstone — a later
write-back over overlapping conversation history can re-extract a fact the learner just deleted.
Documented gap, not fixed this slice; the capped extraction window ages the overlap out over time.
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
