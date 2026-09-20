"""Turning a proven identity into a Guru learner (S21).

Clerk answers "who is this?". This module answers the three questions Clerk cannot: is this
somebody we already know, are they allowed in at all, and is their account still open. That
split is the whole design — a provider that decided who may use Guru would be making the
product's access policy, and swapping providers would then change who can sign in.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
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
    """Which learner this identity is cannot be decided with confidence.

    Two directions, one refusal: the identity's verified addresses name more than one learner,
    or the learner they name already belongs to a *different* identity — including one that
    just won a race against this one for the same account. Either way, never guessed.
    """


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
    """An account this identity already belongs to, by external id or by verified address.

    Every candidate this reads is locked before its ``auth_subject`` decides anything. An
    unclaimed row read and then written to, unlocked, is a row two identities can both claim at
    once — whichever commits last silently wins, with no error and no log, and the loser finds
    out only at their *next* sign-in, as an unexplained lockout. Locked, there are exactly three
    outcomes once the row is re-read: nobody has claimed it (link it), this identity already has
    (a retried exchange is not an error), or a *different* identity already has (refuse — two
    Clerk identities cannot both own one Guru account, and silently moving it is worse than
    refusing a real person).

    The address query deliberately does not filter on ``auth_subject`` the way the old,
    unlocked version did: filtering here would let Postgres's lock-wait re-check (the same
    ``FOR UPDATE`` behaviour ``_create`` relies on) silently drop a row a racing claim just
    took, so the loser would see no candidate at all instead of a taken one — the exact bug this
    replaces, one query over.
    """
    if user.external_id:
        try:
            learner_id = uuid.UUID(user.external_id)
        except ValueError:
            learner_id = None
        if learner_id is not None:
            candidate = await session.scalar(
                select(Learner).where(Learner.id == learner_id).with_for_update()
            )
            if candidate is not None:
                if candidate.auth_subject == user.subject:
                    return candidate
                if candidate.auth_subject is None:
                    candidate.auth_subject = user.subject
                    return candidate
                raise AmbiguousIdentity(candidate.handle)

    addresses = _addresses(user)
    if not addresses:
        return None
    matches = list(
        await session.scalars(select(Learner).where(Learner.email.in_(addresses)).with_for_update())
    )
    mine = [m for m in matches if m.auth_subject == user.subject]
    if mine:
        return mine[0]
    unclaimed = [m for m in matches if m.auth_subject is None]
    if len(unclaimed) > 1:
        raise AmbiguousIdentity(", ".join(sorted(m.handle for m in unclaimed)))
    if unclaimed:
        unclaimed[0].auth_subject = user.subject
        return unclaimed[0]
    if matches:
        # Every address this identity verified already belongs to somebody else — a different
        # identity just won a race for the same address, or simply already owns it. Refused
        # here rather than falling through to `_create`, which would answer the wrong question
        # (`NotInvited`) for an account that already exists.
        raise AmbiguousIdentity(", ".join(sorted(m.handle for m in matches)))
    return None


async def _create(session: AsyncSession, user: ProviderUser) -> Learner:
    """Enroll an invited address, spending its invitation in the same transaction."""
    addresses = _addresses(user)
    invitation = await session.scalar(
        select(Invitation)
        .where(
            # `addresses` is already normalised (`_addresses`, i.e. `normalise_email`, i.e.
            # `.strip().lower()`); `Invitation.email` is matched the same way here — both
            # operations, not just the case fold — rather than trusted to already be stored
            # that way. This module owns the comparison, and it must not depend on every
            # writer of that column (today none; Task 4 is the first) getting it right.
            #
            # `func.trim()` mirrors Python's `.strip()` for the case that actually reaches a
            # stored address — leading/trailing ASCII spaces, e.g. from a copy-paste — but
            # Postgres's bare `TRIM()` strips only the space character by default, where
            # Python's `str.strip()` strips a wider whitespace class (tab, newline, and more).
            # A stored address dirtied by one of *those* would still slip past this filter.
            # Closing that too would mean matching in Python after a broader, unfiltered
            # fetch, which would lock every open invitation on every enrollment rather than
            # only the ones for this identity's own addresses — real contention between
            # unrelated sign-ins, traded for a byte-perfect mirror of a whitespace class an
            # email address's edges are not a realistic place to find. Judged not worth it
            # for a column nothing writes yet.
            func.lower(func.trim(Invitation.email)).in_(addresses),
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

    email = normalise_email(invitation.email)
    learner = Learner(
        handle=await unique_handle(session, handle_for(email)),
        display_name=user.display_name,
        email=email,
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
