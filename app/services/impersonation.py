"""An administrator's read-only visit to a learner's account, always recorded (P10).

Support needs to see what the learner sees. The two ways of doing that without this are asking
for their password and reading the database by hand: the first is never acceptable, the second
leaves no record that anybody looked, and both of them describe what a product does before
somebody decides that acting as another person deserves rules.

Three rules, and each is a failure this exists to prevent rather than a precaution:

**The record is written in the same transaction as the credential.** There is no code path that
grants access and then logs it, so there is no code path where the logging is the part that
failed. That is the whole difference between "we log impersonations" and "impersonation is
logged".

**The session is read-only.** Enforced at the one dependency every authenticated route goes
through (``app.api.deps.get_current_learner``), not per route, because a rule each new endpoint
has to remember is a rule that is eventually not remembered. Support needs to *see* the
account; writing as somebody else puts evidence in their record that they did not create, and
no amount of audit makes that recoverable — the learner's own history becomes something they
cannot trust.

**It is never an administrator.** An impersonated session is refused by the admin and operator
gates whatever the target learner's own flag says. Otherwise impersonating an administrator is
a way for an administrator to launder their own actions through somebody else's name.

The seam: the whole capability is off unless ``GURU_IMPERSONATION_ENABLED`` is set, and with it
off the endpoints answer 404 rather than 403 — a capability that is not enabled should not
announce itself. It is an alpha affordance, and the flag is what makes removing it later a
deployment change rather than an argument.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.auth import Impersonation, LearnerSession
from app.models.learner import Learner

# Short enough to reject "support" and "asked", long enough not to be a puzzle. The length is
# the crude part of the rule and it is deliberate: nothing here can tell a real reason from a
# plausible one, so what it enforces is that somebody had to type a sentence, next to their own
# name, before the credential existed.
MIN_REASON_LENGTH = 8


class ImpersonationError(Exception):
    """Base for refusals the caller turns into a status code."""


class NoSuchLearner(ImpersonationError):
    """There is no learner with that id."""


class ReasonRequired(ImpersonationError):
    """An audit whose every row says "support" records that something happened, not why."""


class CannotImpersonateSelf(ImpersonationError):
    """You already have your own account; recording it as a visit would only muddy the log."""


class NoSuchImpersonation(ImpersonationError):
    """There is no recorded visit with that id."""


@dataclass(frozen=True)
class Began:
    """A visit that has started. The token is returned once and never stored in full."""

    token: str
    expires_at: datetime
    impersonation: Impersonation
    learner: Learner


async def begin(
    session: AsyncSession,
    *,
    admin: Learner,
    learner_id: uuid.UUID,
    reason: str,
    ttl: timedelta,
) -> Began:
    """Start a recorded, read-only session onto ``learner_id`` on behalf of ``admin``.

    The administrator's own session is not touched: this issues a *second* credential rather
    than transforming the first. Ending the visit therefore cannot log them out, and losing the
    visit's token costs them nothing — which is what lets the token be short-lived without
    making support work annoying enough to be routed around.
    """
    reason = reason.strip()
    if len(reason) < MIN_REASON_LENGTH:
        raise ReasonRequired(reason)
    if learner_id == admin.id:
        raise CannotImpersonateSelf(str(learner_id))

    learner = await session.get(Learner, learner_id)
    if learner is None:
        raise NoSuchLearner(str(learner_id))

    now = datetime.now(UTC)
    token = security.new_session_token()
    row = LearnerSession(
        learner_id=learner.id,
        token_hash=security.token_fingerprint(token),
        expires_at=now + ttl,
        last_used_at=now,
        impersonated_by_id=admin.id,
    )
    session.add(row)
    await session.flush()

    # Handles as text beside the ids: the record has to survive either account being closed,
    # and an id whose row is gone identifies nobody.
    record = Impersonation(
        admin_learner_id=admin.id,
        admin_handle=admin.handle,
        learner_id=learner.id,
        learner_handle=learner.handle,
        reason=reason,
        session_id=row.id,
        expires_at=row.expires_at,
    )
    session.add(record)
    await session.commit()
    return Began(token=token, expires_at=row.expires_at, impersonation=record, learner=learner)


async def end(session: AsyncSession, *, impersonation_id: uuid.UUID) -> Impersonation:
    """End a visit by its record, as the administrator rather than as the borrowed account.

    Signing the visit's session out ends it too, and that path is the one a script uses. It is
    the wrong one for a browser: ``/auth/logout`` clears the session cookie on its way out, and
    the cookie in that browser is the administrator's own — so ending a visit would sign them
    out of their own account. This ends it without touching any cookie.

    Any administrator may end any visit. A stray session onto somebody's account is worth more
    closed by whoever noticed it than left open for whoever opened it.
    """
    record = await session.get(Impersonation, impersonation_id)
    if record is None:
        raise NoSuchImpersonation(str(impersonation_id))

    now = datetime.now(UTC)
    if record.session_id is not None:
        await session.execute(
            update(LearnerSession)
            .where(LearnerSession.id == record.session_id, LearnerSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    # Stamped even when the session had already gone: the record is about the visit, and a
    # visit whose credential expired before anybody ended it still ended here.
    if record.ended_at is None:
        record.ended_at = now
    await session.commit()
    return record


async def history(session: AsyncSession, *, limit: int = 100) -> list[Impersonation]:
    """Every recorded visit, newest first.

    Ordered by ``created_at`` and then ``id``: ``now()`` is the transaction clock, so rows
    written by one transaction share it and the order would otherwise fall to a random UUID —
    the same tie behind S56, S14 and S62.
    """
    result = await session.scalars(
        select(Impersonation)
        .order_by(Impersonation.created_at.desc(), Impersonation.id)
        .limit(limit)
    )
    return list(result)


async def for_learner(session: AsyncSession, learner_id: uuid.UUID) -> list[Impersonation]:
    """The visits made to one learner's account, newest first — their half of the record."""
    result = await session.scalars(
        select(Impersonation)
        .where(Impersonation.learner_id == learner_id)
        .order_by(Impersonation.created_at.desc(), Impersonation.id)
    )
    return list(result)
