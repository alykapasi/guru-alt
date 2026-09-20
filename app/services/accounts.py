"""An administrator's acts on accounts: letting somebody in, and stopping them (S21).

Every *local* state change here writes an :class:`~app.models.auth.AccountAction` in the same
transaction as the act, so there is no path that lets somebody in, or shuts an invitation,
without leaving a row that says who did it and to which address. This module exists separately
from ``app.services.identity`` (which spends an invitation) and ``app.services.impersonation``
(which records visits, not account state) because it is where the *administrative* half of
enrollment lives.

The two functions disagree, on purpose, about what happens when the identity provider will not
answer:

- ``invite`` asks the provider **before** writing anything. A provider failure there leaves no
  trace in Guru — no invitation row, no audit row — because the alternative is an invitation
  that unlocks ``/auth/exchange`` (S21 Task 3's enrollment check only reads this table, not
  Clerk's) with nobody ever told it exists. That is a silent side door; recording nothing is the
  safer failure. That ordering has one gap it cannot close: if the provider *succeeds* and the
  commit that follows then fails for any other reason (a dropped connection, a timeout), Clerk
  has already sent a real invitation and Guru still writes nothing — fail-closed (the address
  gets ``NotInvited`` at exchange either way), but otherwise untraceable from Guru's side. That
  one case is logged rather than left silent; see ``invite``.
- ``revoke_invitation`` does the opposite: it commits the local revocation **before** asking the
  provider — or without asking at all, when none is configured — and a provider failure is
  logged rather than raised. An administrator who revoked an invitation has to be able to trust
  that Guru's own gate is shut whether or not Clerk agrees: an invitation Guru still believes is
  open is invisible until the address tries to sign in, where a provider that never got the memo
  is merely an operational loose end an operator can chase from the log line.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import IdentityProvider, ProviderError
from app.models.auth import AccountAction, AccountActionKind, Invitation
from app.models.learner import Learner
from app.services.auth import normalise_email

log = structlog.get_logger(__name__)

# The portal passes the administrator's own row; the CLI has no session to hand over and must
# not invent one (see `app.workers.invite`), so it passes its literal handle instead. Either
# way `_actor_ids` is the one place that turns it into the two columns every audit row carries.
Actor = Learner | str


class AccountsError(Exception):
    """Base for the failures a caller is expected to turn into a status code."""


class AlreadyEnrolled(AccountsError):
    """This address already belongs to an account."""


class AlreadyInvited(AccountsError):
    """This address already has an open invitation."""


class NoSuchInvitation(AccountsError):
    """There is no invitation with that id."""


class NotOpen(AccountsError):
    """This invitation has already been accepted or revoked."""


def _actor_ids(actor: Actor) -> tuple[uuid.UUID | None, str]:
    if isinstance(actor, Learner):
        return actor.id, actor.handle
    return None, actor


async def _refuse_if_not_invitable(session: AsyncSession, address: str) -> None:
    if await session.scalar(select(Learner.id).where(Learner.email == address)) is not None:
        raise AlreadyEnrolled(address)
    open_invitation = await session.scalar(
        select(Invitation.id).where(
            Invitation.email == address,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    if open_invitation is not None:
        raise AlreadyInvited(address)


async def invite(
    session: AsyncSession,
    provider: IdentityProvider | None,
    *,
    actor: Actor,
    email: str,
    notify: bool = True,
) -> Invitation:
    """Issue an invitation to ``email``, storing the address normalised (see module docstring).

    ``notify=False`` (the CLI's ``--no-send``, or a deployment with no provider configured)
    records the invitation without asking the provider to deliver anything — the only case
    ``provider`` may be ``None``. Every other caller, including the portal route, always
    notifies, and the route refuses with 503 before this is ever called without a provider.
    """
    address = normalise_email(email)
    await _refuse_if_not_invitable(session, address)

    provider_invitation_id: str | None = None
    if notify:
        if provider is None:
            raise ProviderError("no identity provider is configured")
        provider_invitation_id = await provider.invite(address)

    actor_id, actor_handle = _actor_ids(actor)
    invitation = Invitation(
        email=address,
        invited_by_learner_id=actor_id,
        invited_by_handle=actor_handle,
        provider_invitation_id=provider_invitation_id,
    )
    session.add(invitation)
    session.add(
        AccountAction(
            actor_learner_id=actor_id,
            actor_handle=actor_handle,
            action=AccountActionKind.INVITE,
            email=address,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # The check above is not locked, so two invitations to the same address can both pass
        # it and race to the partial unique index, which is the real guard. If the provider was
        # already asked (`notify=True`), Clerk is left holding an invitation Guru now refuses to
        # recognise — harmless, since nothing Guru grants depends on Clerk's copy, and the loser
        # here gets the same refusal a second, later request would. Logged anyway, so an
        # operator reconciling Clerk's invitation list against this one can find it.
        await session.rollback()
        if provider_invitation_id:
            log.warning(
                "accounts.invite.provider_invitation_orphaned",
                email=address,
                provider_invitation_id=provider_invitation_id,
                reason="lost the race for the open-invitation slot",
            )
        raise AlreadyInvited(address) from None
    except Exception:
        # Anything else here — a dropped connection, a statement timeout, a full pool — is the
        # one gap the module docstring names: the provider has already been asked and nothing in
        # Guru will say so once this exception propagates. Fail-closed, not silent: this is the
        # only trace left of a Clerk-side invitation nobody in Guru can see.
        await session.rollback()
        if provider_invitation_id:
            log.error(
                "accounts.invite.provider_invitation_abandoned",
                email=address,
                provider_invitation_id=provider_invitation_id,
            )
        raise
    await session.refresh(invitation)
    return invitation


async def list_invitations(session: AsyncSession) -> list[Invitation]:
    """Every invitation ever issued, newest first — open, accepted, and revoked alike."""
    return list(await session.scalars(select(Invitation).order_by(Invitation.created_at.desc())))


async def revoke_invitation(
    session: AsyncSession,
    provider: IdentityProvider | None,
    *,
    actor: Actor,
    invitation_id: uuid.UUID,
) -> Invitation:
    """Close an open invitation, committing locally before the provider is even asked.

    See the module docstring for why this is the opposite ordering from ``invite``: a provider
    that will not answer must not stop an administrator from shutting Guru's own door. ``provider``
    may be ``None`` — no identity provider configured at all — for the same reason: Guru's own
    row is what admits people, so revoking it needs no provider, and refusing to would leave an
    administrator unable to stop an enrollment they can see.
    """
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None:
        raise NoSuchInvitation(str(invitation_id))
    if invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise NotOpen(str(invitation_id))

    actor_id, actor_handle = _actor_ids(actor)
    invitation.revoked_at = datetime.now(UTC)
    invitation.revoked_by_learner_id = actor_id
    session.add(
        AccountAction(
            actor_learner_id=actor_id,
            actor_handle=actor_handle,
            action=AccountActionKind.REVOKE_INVITATION,
            email=invitation.email,
        )
    )
    await session.commit()
    await session.refresh(invitation)

    if invitation.provider_invitation_id:
        if provider is None:
            log.warning(
                "accounts.revoke_invitation.no_provider_configured",
                invitation_id=str(invitation_id),
                provider_invitation_id=invitation.provider_invitation_id,
            )
        else:
            try:
                await provider.revoke_invitation(invitation.provider_invitation_id)
            except ProviderError as exc:
                log.warning(
                    "accounts.revoke_invitation.provider_unreachable",
                    invitation_id=str(invitation_id),
                    error=str(exc),
                )

    return invitation
