"""Notes: the durable, per-learner learning artifact (MASTERPLAN §4.9, Phase 8).

``Note.substrate`` is the meta-wiki: a format-neutral JSONB list of KC-tagged *atoms*
(``{"id", "kind": concept|example|callout|learner, "kc_ids", "md", "provenance"}``) — the
source of truth distillation and edit-absorption update. Rendered notes are cached
projections of it (``NoteRender``), regenerated freely per format. Distinct from
``ContentBlock`` (shared across learners) and ``Memory`` (facts about the learner).
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

# Naive-UTC sentinel for "never distilled" — comparisons against the tz-naive
# LearningEvent/Message created_at columns must stay naive (see app/learning/activity.py).
WATERMARK_EPOCH = datetime(1970, 1, 1)


class Note(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One living note per (learner, topic).

    Catch-up distillation reads two independent activity streams (subject messages and KC
    outcome events), each with its own page limit, so each needs its **own** cursor. A single
    shared watermark advanced to the newest row across both streams, which silently skipped
    the tail of whichever stream its page had truncated.
    """

    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("learner_id", "topic_id"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    substrate: Mapped[list] = mapped_column(JSONB, default=list)
    messages_watermark: Mapped[datetime] = mapped_column(default=WATERMARK_EPOCH)
    events_watermark: Mapped[datetime] = mapped_column(default=WATERMARK_EPOCH)
    # Explicit learner format choice (outline|narrative|mnemonic|worked_examples);
    # NULL = auto (cascade: learned note_format dimension -> heuristic -> outline).
    format: Mapped[str | None] = mapped_column(default=None)
    revision_ordinal: Mapped[int] = mapped_column(default=0)


class NoteRevision(UUIDPrimaryKeyMixin, Base):
    """Append-only substrate snapshot per change. History is never rewritten."""

    __tablename__ = "note_revisions"
    __table_args__ = (UniqueConstraint("note_id", "ordinal"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column()
    substrate: Mapped[list] = mapped_column(JSONB, default=list)
    cause: Mapped[str] = mapped_column()  # distill | learner_edit | restore
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class NoteRender(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Cached projection of one revision into one format. Only current-revision renders kept."""

    __tablename__ = "note_renders"
    __table_args__ = (UniqueConstraint("note_id", "revision_ordinal", "format"),)

    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"), index=True
    )
    revision_ordinal: Mapped[int] = mapped_column()
    format: Mapped[str] = mapped_column()
    content_md: Mapped[str] = mapped_column(Text)
