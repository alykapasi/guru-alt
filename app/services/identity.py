"""Turning a proven identity into a Guru learner (S21).

Clerk answers "who is this?". This module answers the three questions Clerk cannot: is this
somebody we already know, are they allowed in at all, and is their account still open. That
split is the whole design — a provider that decided who may use Guru would be making the
product's access policy, and swapping providers would then change who can sign in.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import IdentityProvider, ProviderUser
from app.models.auth import Invitation
from app.models.learner import Learner
from app.services.auth import handle_for, normalise_email, unique_handle


class EnrollmentError(Exception):
    """This identity does not become a session."""


class NotInvited(EnrollmentError):
    """Nobody has invited any address this identity holds."""


class AccountSuspended(EnrollmentError):
    """The account exists and an administrator has stopped it."""


class AmbiguousIdentity(EnrollmentError):
    """The identity's verified addresses name more than one learner. Never guessed."""


async def enroll(session: AsyncSession, provider: IdentityProvider, subject: str) -> Learner:
    """The learner this provider identity is, creating the account if they were invited.

    Ordered by how *certain* each match is: the provider id we stored ourselves, then the id we
    asked the provider to carry for us, then a verified address, then an invitation. A weaker
    signal never overrides a stronger one, and no signal at all is a refusal rather than a
    guess.
    """
    learner = await session.scalar(select(Learner).where(Learner.auth_subject == subject))
    user = await provider.get_user(subject)
    if learner is None:
        learner = await _link(session, user) or await _create(session, user)
    if learner.suspended_at is not None:
        raise AccountSuspended(str(learner.id))
    await _sync_email(session, learner, user)
    await session.commit()
    await session.refresh(learner)
    return learner


def _addresses(user: ProviderUser) -> list[str]:
    return [normalise_email(address) for address in user.verified_emails]


async def _link(session: AsyncSession, user: ProviderUser) -> Learner | None:
    """An account this identity already belongs to, by external id or by verified address."""
    if user.external_id:
        try:
            learner_id = uuid.UUID(user.external_id)
        except ValueError:
            learner_id = None
        if learner_id is not None:
            candidate = await session.get(Learner, learner_id)
            if candidate is not None and candidate.auth_subject is None:
                candidate.auth_subject = user.subject
                return candidate

    addresses = _addresses(user)
    if not addresses:
        return None
    candidates = list(
        await session.scalars(
            select(Learner).where(Learner.email.in_(addresses), Learner.auth_subject.is_(None))
        )
    )
    if len(candidates) > 1:
        raise AmbiguousIdentity(", ".join(sorted(c.handle for c in candidates)))
    if not candidates:
        return None
    candidates[0].auth_subject = user.subject
    return candidates[0]


async def _create(session: AsyncSession, user: ProviderUser) -> Learner:
    """Enroll an invited address, spending its invitation in the same transaction."""
    addresses = _addresses(user)
    invitation = await session.scalar(
        select(Invitation)
        .where(
            Invitation.email.in_(addresses),
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .order_by(Invitation.created_at)
        .with_for_update()
    )
    if invitation is None:
        # Two sign-ins for one new identity at once, the other branch of the same race the
        # `IntegrityError` below catches. Postgres re-checks a blocked `FOR UPDATE`'s WHERE
        # clause once the row it was waiting on commits (EvalPlanQual): by the time this
        # query unblocks, the winner has already flipped `accepted_at`, so this query finds
        # no open invitation at all rather than a locked one — the loser never reaches the
        # INSERT the other branch guards. Reading by `auth_subject` again catches exactly
        # that case and no other: a different identity arriving after the only invitation
        # was spent still gets `NotInvited`, because no learner carries *its* subject.
        existing = await session.scalar(select(Learner).where(Learner.auth_subject == user.subject))
        if existing is not None:
            return existing
        raise NotInvited(user.subject)

    learner = Learner(
        handle=await unique_handle(session, handle_for(invitation.email)),
        display_name=user.display_name,
        email=invitation.email,
        auth_subject=user.subject,
    )
    session.add(learner)
    try:
        await session.flush()
    except IntegrityError:
        # Two sign-ins for one new identity at once: the unique constraint on `auth_subject`
        # decides, and the loser reads the winner's row rather than failing the person.
        await session.rollback()
        existing = await session.scalar(select(Learner).where(Learner.auth_subject == user.subject))
        if existing is None:
            raise
        return existing
    invitation.accepted_at = datetime.now(UTC)
    invitation.accepted_learner_id = learner.id
    return learner


async def _sync_email(session: AsyncSession, learner: Learner, user: ProviderUser) -> None:
    """Follow an address change at the provider, unless another learner holds that address."""
    addresses = _addresses(user)
    if not addresses or learner.email == addresses[0]:
        return
    taken = await session.scalar(
        select(Learner.id).where(Learner.email == addresses[0], Learner.id != learner.id)
    )
    if taken is None:
        learner.email = addresses[0]
