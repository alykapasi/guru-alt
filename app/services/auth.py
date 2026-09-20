"""Registration, sign-in, and resolving a token back to a learner (S21).

This module is the whole of "who is this request". Everything above it — every route, every
service — already threads ``learner_id``, so the seam being replaced here is one function's
worth of surface. What it is being replaced *with* is the part worth reading: a credential
that is checked rather than assumed, and a session that can be withdrawn.

Three properties are deliberate, and each one is a failure mode somewhere else:

*Sign-in never says which half was wrong.* "No such account" and "wrong password" return the
same error and cost the same time (``app.core.security``), because the difference is a free
list of who has an account here.

*A session is checked on every request, not just issued.* Expiry, revocation and the learner
still existing are all read at use time, so revoking is immediate rather than eventual.

*Registration is the only path that creates a learner.* The stub created one on first sight of
an unauthenticated request, which is exactly the behaviour that makes an auth boundary look
present while being absent.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.auth import Impersonation, LearnerSession
from app.models.learner import Learner

# How stale ``last_used_at`` is allowed to get. Writing it on every request turns an indexed
# read into a write on the hottest row in the request path, for a value nothing makes a
# decision on — it is shown to a learner reviewing their sessions and to an operator.
LAST_USED_RESOLUTION = timedelta(minutes=5)

_HANDLE_MAX = 32


class AuthError(Exception):
    """Base for the failures a caller is expected to turn into a status code."""


def normalise_email(email: str) -> str:
    """The stored form of an address: trimmed and lower-cased.

    Only the case is folded. The local part of an address is case-*sensitive* per RFC 5321 and
    a handful of providers honour that, but treating ``Alice@`` and ``alice@`` as two accounts
    is a reliable way to lock somebody out of their own, so this follows what mail providers
    actually do rather than what the grammar permits.
    """
    return email.strip().lower()


def handle_for(email: str) -> str:
    """A short public identifier derived from an address' local part."""
    local = email.partition("@")[0]
    cleaned = "".join(ch for ch in local if ch.isalnum() or ch in "-_.")[:_HANDLE_MAX]
    return cleaned or "learner"


async def unique_handle(session: AsyncSession, base: str) -> str:
    """``base``, or ``base`` with a short suffix if it is taken.

    Racy by construction — two registrations can pass this check at once — which is why the
    column's unique constraint is the actual guarantee and ``register`` retries on it.
    """
    taken = await session.scalar(select(Learner.id).where(Learner.handle == base))
    if taken is None:
        return base
    return f"{base[: _HANDLE_MAX - 7]}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class IssuedSession:
    """A newly created session: the token goes to the client, the row stays here."""

    token: str
    session_id: uuid.UUID
    expires_at: datetime


async def issue(
    session: AsyncSession, learner: Learner, *, ttl: timedelta, commit: bool = True
) -> IssuedSession:
    """Start a session for ``learner`` and return its token once."""
    now = datetime.now(UTC)
    token = security.new_session_token()
    row = LearnerSession(
        learner_id=learner.id,
        token_hash=security.token_fingerprint(token),
        expires_at=now + ttl,
        last_used_at=now,
    )
    session.add(row)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return IssuedSession(token=token, session_id=row.id, expires_at=row.expires_at)


@dataclass(frozen=True)
class Authenticated:
    """Who a token authenticates, and on whose behalf.

    ``impersonated_by_id`` is not None when an administrator is holding a session onto this
    learner's account (P10). It travels with the learner rather than being looked up again,
    because every caller that has to *restrict* the request already has the session row in
    hand here — and a rule enforced by remembering to ask a second question is a rule that
    gets forgotten at the next endpoint.
    """

    learner: Learner
    impersonated_by_id: uuid.UUID | None
    session_id: uuid.UUID | None = None


async def resolve_session(
    session: AsyncSession, token: str, *, impersonation_enabled: bool | None = None
) -> Authenticated | None:
    """The learner this token authenticates and how, or ``None``.

    ``None`` covers every reason equally — unknown, expired, revoked, or belonging to a
    learner who no longer exists — because the caller's response to all of them is the same
    401, and distinguishing them for the client says more than it needs to.
    """
    if not token:
        return None
    now = datetime.now(UTC)
    row = await session.scalar(
        select(LearnerSession).where(LearnerSession.token_hash == security.token_fingerprint(token))
    )
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    learner = await session.get(Learner, row.learner_id)
    if learner is None:
        return None
    if row.impersonated_by_id is not None:
        if impersonation_enabled is None:
            from app.core.config import get_settings

            impersonation_enabled = get_settings().impersonation_enabled
        admin = await session.get(Learner, row.impersonated_by_id, populate_existing=True)
        # The *administrator's* suspension ends their visits, not the learner's: a visit is the
        # administrator's credential, and support has to be able to keep looking at exactly the
        # account that is in trouble (S21).
        if (
            not impersonation_enabled
            or admin is None
            or not admin.is_admin
            or admin.suspended_at is not None
        ):
            return None
    elif learner.suspended_at is not None:
        # Suspension has to bite a session that already exists, not only stop a future sign-in
        # — a check that only ran at sign-in would leave a suspended learner working until their
        # cookie expired, which is exactly when Guru least wants that (S21). This runs on every
        # authenticated request, same as expiry and revocation above.
        return None
    if now - row.last_used_at >= LAST_USED_RESOLUTION:
        row.last_used_at = now
        await session.commit()
    return Authenticated(
        learner=learner, impersonated_by_id=row.impersonated_by_id, session_id=row.id
    )


async def resolve(session: AsyncSession, token: str) -> Learner | None:
    """The learner this token authenticates, or ``None``. Says nothing about who is holding it."""
    resolved = await resolve_session(session, token)
    return resolved.learner if resolved is not None else None


async def _close_impersonations(session: AsyncSession, session_ids: Sequence[uuid.UUID]) -> None:
    """Stamp the audit rows for sessions that have just been revoked (P10).

    Here rather than in the impersonation service, because *every* way a session ends has to
    close its record — and there is exactly one of those, which is this module. An end recorded
    only by the endpoint that happens to be called politely is an end that goes unrecorded the
    first time somebody signs out instead.
    """
    if not session_ids:
        return
    await session.execute(
        update(Impersonation)
        .where(Impersonation.session_id.in_(session_ids), Impersonation.ended_at.is_(None))
        .values(ended_at=datetime.now(UTC))
    )


async def revoke(session: AsyncSession, token: str) -> bool:
    """End the session this token names. Returns whether one was live to end."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(LearnerSession)
        .where(
            LearnerSession.token_hash == security.token_fingerprint(token),
            LearnerSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(LearnerSession.id)
    )
    ended = list(result.scalars())
    await _close_impersonations(session, ended)
    await session.commit()
    return bool(ended)


async def revoke_all(session: AsyncSession, learner_id: uuid.UUID) -> int:
    """End every live session for a learner. Returns how many were ended."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(LearnerSession)
        .where(LearnerSession.learner_id == learner_id, LearnerSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(LearnerSession.id)
    )
    ended = list(result.scalars())
    await _close_impersonations(session, ended)
    await session.commit()
    return len(ended)


async def purge_expired(session: AsyncSession, *, keep_revoked_for: timedelta) -> int:
    """Delete sessions that can no longer authenticate anybody.

    A revoked row is kept for a while after revocation rather than deleted at once: while it
    exists, presenting the token is distinguishable from presenting a token that never existed,
    which is the difference between "your session was ended" and a silent failure nobody can
    diagnose.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        delete(LearnerSession).where(
            (LearnerSession.expires_at <= now)
            | (LearnerSession.revoked_at <= now - keep_revoked_for)
        )
    )
    await session.commit()
    return int(cast("CursorResult[Any]", result).rowcount)


async def sessions_for(session: AsyncSession, learner_id: uuid.UUID) -> list[LearnerSession]:
    """Live sessions for a learner, newest first."""
    now = datetime.now(UTC)
    result = await session.scalars(
        select(LearnerSession)
        .where(
            LearnerSession.learner_id == learner_id,
            LearnerSession.revoked_at.is_(None),
            LearnerSession.expires_at > now,
        )
        .order_by(LearnerSession.created_at.desc())
    )
    return list(result)
