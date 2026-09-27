"""A learner's explicit settings (S02, V09): one row per choice, NULL subject = global.

A row exists only for an explicit choice. Globally, the catalog default needs no row; a subject
row may hold the default too ("guided here", "adapt to me here"), because that differs from a
global setting that says otherwise. Clearing a level deletes its row.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LearnerPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "learner_preferences"
    __table_args__ = (
        Index(
            "uq_learner_preferences_scope_key",
            "learner_id",
            "subject_id",
            "key",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), default=None
    )
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
