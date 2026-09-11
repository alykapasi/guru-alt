"""Per-learner learning state and the immutable event log.

These tables are defined now so the schema is stable; the mastery estimator (Phase 3)
populates ``LearnerKCState``, and ``LearningEvent`` is the KC-tagged, replayable log
that later feeds the tracer, the learner profile, and (eventually) DKT.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LearnerKCState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Continuous mastery estimate for one (learner, KC) pair."""

    __tablename__ = "learner_kc_state"
    __table_args__ = (UniqueConstraint("learner_id", "kc_id"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    kc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    ability: Mapped[float] = mapped_column(default=0.0)
    uncertainty: Mapped[float] = mapped_column(default=1.0)
    # Real UTC instants — the tracer does elapsed-time math (decay, FSRS) on these.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    # Opaque serialized FSRS card (stability/difficulty/state) — see app.learning.scheduler.
    fsrs_card: Mapped[dict | None] = mapped_column(JSONB, default=None)


class LearningEvent(UUIDPrimaryKeyMixin, Base):
    """An immutable, KC-tagged record of one learner interaction.

    Append-only — no ``updated_at``. The ``payload`` shape grows as later phases add
    richer signals (score, latency, hints, …).
    """

    __tablename__ = "learning_events"
    # An ``attempt_id`` supplied by a caller is an idempotency key: a retried submission must
    # update mastery once, not once per retry. Uniqueness is per (learner, attempt, KC) because
    # one attempt legitimately writes one row per tagged KC. Partial, since ``attempt_id`` is
    # NULL on pre-migration rows and on non-attempt events, which are not deduplicated.
    __table_args__ = (
        Index(
            "uq_learning_events_learner_attempt_kc",
            "learner_id",
            "attempt_id",
            "kc_id",
            unique=True,
            postgresql_where=text("attempt_id IS NOT NULL"),
        ),
        # "has this learner answered this item, and when" — asked once per candidate item
        # every time practice picks a question (S14). The item id lives in the payload rather
        # than a column, so without an expression index the lookup is a scan of every
        # observation the learner has ever produced. Partial, because only observations
        # carry an item.
        Index(
            "ix_learning_events_learner_item",
            "learner_id",
            text("(payload ->> 'item_id')"),
            postgresql_where=text("event_type = 'observation'"),
        ),
    )

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    kc_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kcs.id", ondelete="SET NULL"), index=True, default=None
    )
    event_type: Mapped[str] = mapped_column(index=True)
    # One graded answer fans out into one row per tagged KC — same score, latency, hints and
    # item, differing only in ``payload["weight"]``. ``attempt_id`` ties that fan-out back
    # together so anything measuring the *learner's action* (activity volume, latency, hints,
    # format effectiveness) counts it once, while each KC keeps its own evidence row.
    # NULL on rows written before the column existed, and on non-attempt events like
    # ``placement_seed``; treat such a row as its own attempt.
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
