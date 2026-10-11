"""Frozen evaluation examples and the intent decisions they can be drawn from (S59).

An example is a *copy*: the inputs a labeller sees and the decision a model made, frozen when
it was drawn, so later edits to the source cannot change what was labelled. The source is
referenced, never joined, for the same reason — the label is about the snapshot.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import UUIDPrimaryKeyMixin

AREAS = ("grading", "diagnosis", "intent", "source_support")


class EvalExample(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "eval_examples"
    __table_args__ = (
        UniqueConstraint("area", "source_kind", "source_id", name="uq_eval_examples_source"),
    )

    area: Mapped[str] = mapped_column(index=True)
    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    source_kind: Mapped[str] = mapped_column()  # event | message | intent
    source_id: Mapped[uuid.UUID] = mapped_column()
    snapshot: Mapped[dict] = mapped_column(JSONB)
    decision: Mapped[dict] = mapped_column(JSONB)
    label: Mapped[dict | None] = mapped_column(JSONB, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    labelled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), default=None
    )
    labelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class IntentRecord(UUIDPrimaryKeyMixin, Base):
    """What the practice gate decided a reply did about the open question (S59).

    Written only for eval-eligible accounts: the gate's decision was otherwise never kept
    unless Jev was asking the same question, so there was nothing to sample or label.
    """

    __tablename__ = "intent_records"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, default=None
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), default=None
    )
    stem: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
