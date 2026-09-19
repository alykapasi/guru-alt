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

from sqlalchemy import JSON, DateTime, ForeignKey, func
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
    # Who is *really* holding this session, when that is not its learner (P10). NULL for every
    # ordinary session, which is almost all of them.
    #
    # The flag lives here rather than only on ``impersonations`` because every request resolves
    # this row already, and the alternative is a join on the hot path of every authenticated
    # call to answer a question whose answer is NULL for almost all of them. The audit row is
    # the *record*. Deleting the actor revokes their borrowed sessions via CASCADE;
    # SET NULL would silently turn a borrowed token into an ordinary learner credential.
    impersonated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True, default=None
    )


class Impersonation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One administrator's audited visit to one learner's account, recorded (P10).

    The audit is the *product*, not a side effect of it: ``impersonation.begin`` writes this
    row and issues the credential in the same transaction, so there is no path that grants
    access without leaving a record. That is the whole difference between "we log
    impersonations" and "impersonation is logged".

    **It has to outlive both accounts, which is why the ids are nullable and the handles are
    not.** A record that a deleted learner can erase is not an audit of access to that
    learner's data, and one an administrator can erase by closing their own account is not an
    audit of anything at all. So both foreign keys are SET NULL and the handles are captured
    here as text at the time. The learner's is the one that has to go on deletion, and the
    service clears it — see ``app.services.retention``, which states both halves.

    ``ended_at`` means *explicitly* ended, by signing the impersonated session out. NULL does
    not mean "still running": an untouched visit simply expires, and ``expires_at`` is the
    outer bound either way. Recording a wall-clock end that nobody performed would be inventing
    an event.
    """

    __tablename__ = "impersonations"

    admin_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    admin_handle: Mapped[str] = mapped_column()
    learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    learner_handle: Mapped[str | None] = mapped_column(default=None)
    # Required by the endpoint, and required to be more than a couple of characters: an audit
    # whose every row says "support" records that something happened and nothing about why.
    reason: Mapped[str] = mapped_column()
    # The credential this record issued. SET NULL because sessions are purged once they can no
    # longer authenticate anybody, and the audit must not be purged with them.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "learner_sessions.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"
        ),
        default=None,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


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


class AdminAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable request intent; no credentials, request bodies, or query strings."""

    __tablename__ = "admin_actions"

    impersonation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("impersonations.id"), index=True)
    method: Mapped[str] = mapped_column()
    route: Mapped[str] = mapped_column()
    resource_ids: Mapped[dict[str, str]] = mapped_column(JSON)
    status_code: Mapped[int | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
