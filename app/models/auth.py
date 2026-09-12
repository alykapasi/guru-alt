"""Server-side sessions (S21).

The session is a database row, not a signed claim. That is deliberate: a self-contained
signed token cannot be withdrawn before it expires, so "log out everywhere", "revoke the
session on that stolen laptop" and account deletion all become promises the server cannot
keep. A row can be deleted. The cost is a lookup per request, which is one indexed read on a
64-character key.

``token_hash`` is a SHA-256 fingerprint, never the token itself — see ``app.core.security``.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LearnerSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One authenticated session belonging to one learner."""

    __tablename__ = "learner_sessions"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    # Unique so a fingerprint collision surfaces as an integrity error rather than as one
    # learner resolving to another's session.
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Rewritten on use, coarsely (see ``auth.resolve``): it exists to show an operator and a
    # learner which sessions are live, not to measure traffic.
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set rather than deleted, so a revoked session stays distinguishable from one that was
    # never issued for as long as the row is retained.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
