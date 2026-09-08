"""The learner profile: a behavior-derived model of *how* a learner learns (MASTERPLAN §4.8).

Complements ``LearnerKCState`` (*what* they know). ``ProfileDimension`` is deliberately EAV-
shaped (one row per ``(learner_id, key)``) rather than a fixed-column table: the dimension
catalog lives in code (``app/learning/profile_estimators.py``), so adding or dropping a
dimension never needs a migration — the whole point, since the catalog is expected to be
pruned once real usage data shows which dimensions are actually predictive.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LearnerProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Header row, one per learner. The dimensions themselves live in ``ProfileDimension``."""

    __tablename__ = "learner_profiles"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), unique=True, index=True
    )
    # The newest piece of evidence the last refresh actually read. A refresh recomputes every
    # dimension from the whole history, so repeating it over unchanged evidence buys nothing
    # and costs several model calls; this is what makes "nothing new" answerable without
    # paying to find out (S43).
    evidence_watermark: Mapped[datetime | None] = mapped_column(default=None)
    # When a refresh last completed, and why the last one did not. Without these, a profile
    # that silently stopped updating is indistinguishable from one nothing has changed for.
    refreshed_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class ProfileDimension(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One estimated dimension value for a learner (e.g. ``pace``, ``interests``).

    ``value`` is arbitrary JSON — a float, a list, or a small dict — whatever shape that
    dimension's estimator produces (see ``profile_estimators.DIMENSION_SPECS``). ``kind``
    (trait|state) and ``source`` (behavioral|self_report) are descriptive metadata, not an
    update mechanism: every refresh recomputes the value from scratch (traits read the full
    event history, states read only the most recent session).
    """

    __tablename__ = "profile_dimensions"
    __table_args__ = (UniqueConstraint("learner_id", "key"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(index=True)
    value: Mapped[Any] = mapped_column(JSONB)
    uncertainty: Mapped[float] = mapped_column(default=1.0)
    kind: Mapped[str] = mapped_column()  # "trait" | "state"
    source: Mapped[str] = mapped_column()  # "behavioral" | "self_report"
