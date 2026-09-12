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

from sqlalchemy import DateTime, ForeignKey, func
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


class PasswordResetToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single-use, short-lived permission to set a new password (S21).

    Stored as a fingerprint, never in full, for the same reason as a session token: a database
    dump or a log line must not be a way in. It is a *separate* table from ``learner_sessions``
    rather than a flag on one, because the two have opposite properties — a session is long
    and renewable, a reset is short and spent on first use — and sharing a row would mean the
    weaker rules governing both.
    """

    __tablename__ = "password_reset_tokens"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set rather than deleted, so a token presented twice is distinguishable from one that
    # never existed for as long as the row is kept — the same argument as a revoked session.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SignInAttempt(UUIDPrimaryKeyMixin, Base):
    """One failed sign-in, kept only long enough to throttle the next one (S21).

    Recorded in the database rather than in a process's memory on purpose. An in-memory counter
    is per-process, so a deployment behind two workers gives an attacker twice the budget and a
    restart gives them a fresh one — which is to say it throttles the honest user who mistyped
    and nobody else.

    Both the address and the client are recorded, because they are two different attacks.
    Repeated failures against one address is somebody working on one account; repeated failures
    from one client across many addresses is credential stuffing, and the per-address counter
    never sees it.

    Only *failures* are written. A successful sign-in costs no write, so the common path is
    unchanged, and being expensive is the entire point of the uncommon one.
    """

    __tablename__ = "sign_in_attempts"

    # Normalised, and not a foreign key: an attempt against an address nobody has registered is
    # exactly the kind that most needs counting, and a FK would make it unrecordable.
    email: Mapped[str] = mapped_column(index=True)
    client: Mapped[str] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
