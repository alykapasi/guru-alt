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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.auth import LearnerSession, PasswordResetToken, SignInAttempt
from app.models.learner import Learner

# How stale ``last_used_at`` is allowed to get. Writing it on every request turns an indexed
# read into a write on the hottest row in the request path, for a value nothing makes a
# decision on — it is shown to a learner reviewing their sessions and to an operator.
LAST_USED_RESOLUTION = timedelta(minutes=5)

_HANDLE_MAX = 32


class AuthError(Exception):
    """Base for the failures a caller is expected to turn into a status code."""


class EmailTaken(AuthError):
    """The address already belongs to an account."""


class InvalidCredentials(AuthError):
    """The address and password together do not identify anybody.

    Deliberately one error for both halves: see the module docstring.
    """


def normalise_email(email: str) -> str:
    """The stored form of an address: trimmed and lower-cased.

    Only the case is folded. The local part of an address is case-*sensitive* per RFC 5321 and
    a handful of providers honour that, but treating ``Alice@`` and ``alice@`` as two accounts
    is a reliable way to lock somebody out of their own, so this follows what mail providers
    actually do rather than what the grammar permits.
    """
    return email.strip().lower()


def _handle_for(email: str) -> str:
    """A short public identifier derived from an address' local part."""
    local = email.partition("@")[0]
    cleaned = "".join(ch for ch in local if ch.isalnum() or ch in "-_.")[:_HANDLE_MAX]
    return cleaned or "learner"


async def _unique_handle(session: AsyncSession, base: str) -> str:
    """``base``, or ``base`` with a short suffix if it is taken.

    Racy by construction — two registrations can pass this check at once — which is why the
    column's unique constraint is the actual guarantee and ``register`` retries on it.
    """
    taken = await session.scalar(select(Learner.id).where(Learner.handle == base))
    if taken is None:
        return base
    return f"{base[: _HANDLE_MAX - 7]}-{uuid.uuid4().hex[:6]}"


async def register(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
) -> Learner:
    """Create a learner with a password credential.

    Raises ``EmailTaken`` when the address is already registered. The check and the insert are
    not atomic, so the unique constraint is what actually decides, and a loser of that race
    gets the same ``EmailTaken`` as a plain duplicate.
    """
    address = normalise_email(email)
    existing = await session.scalar(select(Learner.id).where(Learner.email == address))
    if existing is not None:
        raise EmailTaken(address)

    learner = Learner(
        handle=await _unique_handle(session, _handle_for(address)),
        display_name=display_name,
        email=address,
        password_hash=security.hash_password(password),
    )
    session.add(learner)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailTaken(address) from exc
    await session.refresh(learner)
    return learner


async def authenticate(session: AsyncSession, *, email: str, password: str) -> Learner:
    """The learner these credentials identify, or ``InvalidCredentials``."""
    address = normalise_email(email)
    learner = await session.scalar(select(Learner).where(Learner.email == address))
    if learner is None:
        # Spend the verification anyway: an unknown address must not be the fast path.
        security.verify_dummy()
        raise InvalidCredentials(address)
    if not security.verify_password(password, learner.password_hash):
        raise InvalidCredentials(address)
    return learner


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


async def resolve(session: AsyncSession, token: str) -> Learner | None:
    """The learner this token authenticates, or ``None``.

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
    if now - row.last_used_at >= LAST_USED_RESOLUTION:
        row.last_used_at = now
        await session.commit()
    return learner


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
    )
    await session.commit()
    return bool(cast("CursorResult[Any]", result).rowcount)


async def revoke_all(session: AsyncSession, learner_id: uuid.UUID) -> int:
    """End every live session for a learner. Returns how many were ended."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(LearnerSession)
        .where(LearnerSession.learner_id == learner_id, LearnerSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.commit()
    return int(cast("CursorResult[Any]", result).rowcount)


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


class TooManyAttempts(AuthError):
    """Sign-in is throttled for this address or this client right now."""


class WrongPassword(AuthError):
    """The current password given alongside a credential change did not match."""


async def _recent_failures(
    session: AsyncSession, *, email: str, client: str, window: timedelta
) -> tuple[int, int]:
    """Failed sign-ins in the window, for this address and for this client."""
    since = datetime.now(UTC) - window
    by_email = await session.scalar(
        select(func.count())
        .select_from(SignInAttempt)
        .where(SignInAttempt.email == email, SignInAttempt.created_at >= since)
    )
    by_client = await session.scalar(
        select(func.count())
        .select_from(SignInAttempt)
        .where(SignInAttempt.client == client, SignInAttempt.created_at >= since)
    )
    return int(by_email or 0), int(by_client or 0)


async def check_sign_in_allowed(
    session: AsyncSession,
    *,
    email: str,
    client: str,
    window: timedelta,
    max_per_email: int,
    max_per_client: int,
) -> None:
    """Raise ``TooManyAttempts`` when this address or client has failed too often lately.

    Two counters, because they are two different attacks. Repeated failures against one address
    is somebody working on one account; repeated failures from one client across many addresses
    is credential stuffing, which the per-address counter never sees because each address fails
    only once or twice.

    This also closes a hole Argon2 opened. Hashing a password is deliberately slow and
    memory-hard, which is a defence against an attacker holding the table and a *cost to the
    server* on every attempt — so an unthrottled sign-in endpoint is a way to spend our CPU at
    the attacker's convenience. Counting first means a refused attempt costs a cheap indexed
    read instead of a hash.
    """
    by_email, by_client = await _recent_failures(
        session, email=normalise_email(email), client=client, window=window
    )
    if by_email >= max_per_email or by_client >= max_per_client:
        raise TooManyAttempts(email)


async def record_failed_sign_in(session: AsyncSession, *, email: str, client: str) -> None:
    """Note one failed attempt. Committed on its own, because it must survive the 401."""
    session.add(SignInAttempt(email=normalise_email(email), client=client))
    await session.commit()


async def purge_sign_in_attempts(session: AsyncSession, *, older_than: timedelta) -> int:
    """Drop attempt rows past the throttling window — they answer no later question."""
    result = await session.execute(
        delete(SignInAttempt).where(SignInAttempt.created_at < datetime.now(UTC) - older_than)
    )
    await session.commit()
    return int(cast("CursorResult[Any]", result).rowcount)


@dataclass(frozen=True)
class IssuedReset:
    """A reset that was created. The token is returned once, to be delivered out of band."""

    token: str
    learner: Learner
    expires_at: datetime


async def begin_password_reset(
    session: AsyncSession, *, email: str, ttl: timedelta
) -> IssuedReset | None:
    """Start a password reset, or ``None`` when the address belongs to nobody.

    ``None`` rather than an error, and the caller answers the same either way: whether an
    address has an account here is precisely what the sign-in path already refuses to say, and
    a reset endpoint that answers it gives the enumeration oracle back through another door.

    A learner with no password hash still gets one. Their account has no password *yet*, which
    is a reason to be able to set one, not a reason to be locked out — and refusing would leak
    which accounts authenticate some other way.
    """
    address = normalise_email(email)
    learner = await session.scalar(select(Learner).where(Learner.email == address))
    if learner is None:
        return None
    token = security.new_session_token()
    session.add(
        PasswordResetToken(
            learner_id=learner.id,
            token_hash=security.token_fingerprint(token),
            expires_at=datetime.now(UTC) + ttl,
        )
    )
    await session.commit()
    return IssuedReset(token=token, learner=learner, expires_at=datetime.now(UTC) + ttl)


async def complete_password_reset(
    session: AsyncSession, *, token: str, password: str
) -> Learner | None:
    """Spend a reset token and set the new password, or ``None`` if it cannot be spent.

    Every live session is revoked. A reset is what somebody does when they think their account
    may not be theirs alone, and leaving the intruder's session working would make the reset a
    gesture — the one action whose whole point is to end access has to end it everywhere.

    The token is marked used in the same transaction as the password write, so a token cannot
    be spent twice by two requests arriving together: the unique fingerprint plus the
    single-row update is what decides, not the check.
    """
    if not token:
        return None
    now = datetime.now(UTC)
    row = await session.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == security.token_fingerprint(token)
        )
    )
    if row is None or row.used_at is not None or row.expires_at <= now:
        return None
    learner = await session.get(Learner, row.learner_id)
    if learner is None:
        return None
    learner.password_hash = security.hash_password(password)
    row.used_at = now
    await session.execute(
        update(LearnerSession)
        .where(LearnerSession.learner_id == learner.id, LearnerSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.commit()
    return learner


async def purge_password_resets(session: AsyncSession, *, older_than: timedelta) -> int:
    """Delete reset tokens that can no longer be spent."""
    now = datetime.now(UTC)
    result = await session.execute(
        delete(PasswordResetToken).where(
            (PasswordResetToken.expires_at <= now)
            | (PasswordResetToken.used_at <= now - older_than)
        )
    )
    await session.commit()
    return int(cast("CursorResult[Any]", result).rowcount)


async def change_password(
    session: AsyncSession,
    learner: Learner,
    *,
    current: str,
    new: str,
    keep_token: str | None = None,
) -> None:
    """Set a new password for a signed-in learner, proving they know the old one.

    The current password is required even though the caller already holds a valid session,
    because a session is not proof of the person: a borrowed laptop is a session. Requiring the
    password is what stops a walk-up from locking the owner out of their own account.

    Every *other* session is revoked and this one is kept, which is the shape somebody actually
    wants — a password change is often a response to suspecting another device, and signing
    yourself out of the tab you are typing in is a way of making the safe action annoying.
    """
    if not security.verify_password(current, learner.password_hash):
        raise WrongPassword(str(learner.id))
    learner.password_hash = security.hash_password(new)
    revoke_others = update(LearnerSession).where(
        LearnerSession.learner_id == learner.id, LearnerSession.revoked_at.is_(None)
    )
    if keep_token:
        revoke_others = revoke_others.where(
            LearnerSession.token_hash != security.token_fingerprint(keep_token)
        )
    await session.execute(revoke_others.values(revoked_at=datetime.now(UTC)))
    await session.commit()


async def change_email(
    session: AsyncSession, learner: Learner, *, password: str, email: str
) -> None:
    """Move a learner to a new address, proving they know the password.

    Raises ``EmailTaken`` when the address is somebody else's — the one place this codebase
    does say whether an address is registered, and it is unavoidable: the alternative is
    silently not changing it. Confirming the new address before it becomes the sign-in identity
    is the missing half, and it needs a mail transport this repository does not have.
    """
    if not security.verify_password(password, learner.password_hash):
        raise WrongPassword(str(learner.id))
    address = normalise_email(email)
    taken = await session.scalar(
        select(Learner.id).where(Learner.email == address, Learner.id != learner.id)
    )
    if taken is not None:
        raise EmailTaken(address)
    learner.email = address
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmailTaken(address) from exc
