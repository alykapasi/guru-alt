"""The learner. Identity is a stub for the MVP (see docs/MASTERPLAN §7, auth).

``handle`` is the stub identity key; real auth (Phase 8) maps an external identity to
a Learner without changing anything downstream that already threads ``learner_id``.
"""

from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Learner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An end user of the platform."""

    __tablename__ = "learners"

    handle: Mapped[str] = mapped_column(unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(default=None)
