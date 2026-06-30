"""Per-learner learning state and the immutable event log.

These tables are defined now so the schema is stable; the mastery estimator (Phase 3)
populates ``LearnerKCState``, and ``LearningEvent`` is the KC-tagged, replayable log
that later feeds the tracer, the learner profile, and (eventually) DKT.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
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

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    kc_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kcs.id", ondelete="SET NULL"), index=True, default=None
    )
    event_type: Mapped[str] = mapped_column(index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
