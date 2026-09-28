"""Frozen copies of what a grade was measured against (S56)."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class GradingSnapshot(Base):
    """An item, rubric or grading prompt as it was when a grade used it.

    Keyed by the SHA-256 of its canonical JSON, per learner: written once, never updated, and
    deleted only with the learner. Events reference it by hash (``payload["grading"]``); there
    is no foreign key to ``items`` or ``rubrics`` on purpose, so deleting those keeps history.
    """

    __tablename__ = "grading_snapshots"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # item | rubric | prompt
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
