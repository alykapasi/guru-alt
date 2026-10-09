"""What could not be erased when asked, retried until it is (S61, V12).

Names no learner — a blob key or an identity-provider subject — so it survives the account
erase it belongs to, and is deleted once the erasure succeeds.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import UUIDPrimaryKeyMixin


class ErasureKind(StrEnum):
    BLOB = "blob"
    IDENTITY = "identity"
    CHECKPOINT = "checkpoint"


class PendingErasure(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pending_erasures"
    __table_args__ = (UniqueConstraint("kind", "target", name="uq_pending_erasures_kind_target"),)

    kind: Mapped[str] = mapped_column()  # ErasureKind
    target: Mapped[str] = mapped_column()
    attempts: Mapped[int] = mapped_column(server_default="0", default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
