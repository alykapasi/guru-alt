"""The administrator's read and control of this deployment (P10, S21).

Split from ``ops`` on purpose, and the split is about *what is being read* rather than how
sensitive it feels. ``/ops/*`` reports aggregates — spend, queue depth, which conditions are
firing — and admits either an administrator or a monitor holding a shared static token. This
reports people: who registered, what they use, what their use costs. A shared secret sitting in
a monitor's configuration is the wrong credential for that, so these take an administrator's
session and nothing else.

No longer read-only. Impersonation was the first exception; invitations (S21) are the second,
and every write either of them makes is a durable record — a visit for one, an
``AccountAction`` for the other — written in the same transaction as the act itself. A portal
that can only look is a portal that cannot yet be used to do something nobody recorded; this
one can be, and everything below stays true to that.
"""

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentAdmin, IdentityProviderDep, SessionDep, SettingsDep
from app.core.identity import ProviderError
from app.schemas.admin import (
    AdminActionRead,
    ImpersonationRead,
    ImpersonationRequest,
    ImpersonationStarted,
    InvitationCreate,
    InvitationRead,
)
from app.services import accounts, impersonation
from app.services.admin import LearnerRoster, learner_usage

router = APIRouter(prefix="/admin", tags=["admin"])

_PROVIDER_UNAVAILABLE = "sign-in is not configured on this server"


@router.get("/learners", response_model=LearnerRoster)
async def learners(
    _: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    hours: Annotated[int | None, Query(ge=1, le=24 * 365)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Everyone with an account, most expensive first, with what they did in the window.

    Defaults to the same window as ``/ops/spend`` so the per-learner figures add up to
    something a reader has already seen, rather than to a total from a different fortnight.

    A learner with no calls in the window is still listed, with zeroes. They are the row worth
    reading: somebody registered and did not come back, and leaving them out would make the
    deployment look healthier than it is.

    ``total`` is every account rather than the number returned, because the ordering puts those
    quiet learners last and the cap then removes them — a truncated page and a complete one are
    indistinguishable without it.
    """
    window_hours = hours if hours is not None else settings.spend_window_hours
    return await learner_usage(session, hours=window_hours, limit=limit)


@router.post("/impersonate", response_model=ImpersonationStarted)
async def impersonate(
    body: ImpersonationRequest,
    admin: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
):
    """Start a recorded administrator session onto one learner's account (P10).

    404 rather than 403 when the capability is switched off, because a capability a deployment
    has not enabled should not announce that it exists.

    The token is returned once and is a *second* credential — the administrator's own session
    is untouched, so ending the visit cannot sign them out and losing its token costs them
    nothing. Signing that session out ends the visit and stamps the record; nothing else needs
    to be called, which is deliberate, because an end that depends on the polite endpoint being
    used is an end that goes unrecorded the first time somebody just logs out.
    """
    if not settings.impersonation_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        began = await impersonation.begin(
            session,
            admin=admin,
            learner_id=body.learner_id,
            reason=body.reason,
            ttl=timedelta(minutes=settings.impersonation_ttl_minutes),
        )
    except impersonation.NoSuchLearner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such learner") from None
    except impersonation.ReasonRequired:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "say why, in a sentence"
        ) from None
    except impersonation.CannotImpersonateSelf:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that is your own account") from None

    return ImpersonationStarted(
        token=began.token,
        expires_at=began.expires_at,
        learner_id=began.learner.id,
        learner_handle=began.learner.handle,
        impersonation=ImpersonationRead.model_validate(began.impersonation),
    )


@router.get("/impersonations", response_model=list[ImpersonationRead])
async def impersonations(
    _: CurrentAdmin,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Every recorded visit, newest first.

    Listed whether or not the capability is currently enabled: turning it off must not hide
    what was done while it was on, which would make the switch a way to erase the record
    rather than a way to withdraw the power.
    """
    return await impersonation.history(session, limit=limit)


@router.delete("/impersonations/{impersonation_id}", response_model=ImpersonationRead)
async def end_impersonation(
    impersonation_id: uuid.UUID,
    _: CurrentAdmin,
    session: SessionDep,
):
    """End a visit, as yourself rather than as the account being viewed.

    Signing the visit's own session out ends it too, and that is the path a script uses. It is
    the wrong one for a browser: `/auth/logout` clears the session cookie on its way out, and
    the cookie in that browser belongs to the administrator — so ending a visit that way would
    sign them out of their own account.

    Not gated on the capability being enabled, for the same reason the log is not: switching it
    off must not leave a live visit that nobody can close.
    """
    try:
        return await impersonation.end(session, impersonation_id=impersonation_id)
    except impersonation.NoSuchImpersonation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such visit") from None


@router.get("/impersonations/{impersonation_id}/actions", response_model=list[AdminActionRead])
async def actions(impersonation_id: uuid.UUID, _: CurrentAdmin, session: SessionDep):
    from sqlalchemy import select

    from app.models.auth import AdminAction, Impersonation

    if await session.get(Impersonation, impersonation_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such visit")
    return list(
        await session.scalars(
            select(AdminAction)
            .where(AdminAction.impersonation_id == impersonation_id)
            .order_by(AdminAction.created_at.desc(), AdminAction.id)
        )
    )


@router.post("/invitations", response_model=InvitationRead, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InvitationCreate,
    admin: CurrentAdmin,
    session: SessionDep,
    provider: IdentityProviderDep,
):
    """Invite one address to enroll (S21).

    The provider is asked to send the invitation before anything is written — see
    ``app.services.accounts.invite`` for why that ordering is deliberate.
    """
    if provider is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _PROVIDER_UNAVAILABLE)
    try:
        return await accounts.invite(session, provider, actor=admin, email=str(body.email))
    except accounts.AlreadyEnrolled:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that address already has an account"
        ) from None
    except accounts.AlreadyInvited:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "that address already has an open invitation"
        ) from None
    except ProviderError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "the sign-in provider could not be reached; try again",
        ) from exc


@router.get("/invitations", response_model=list[InvitationRead])
async def invitations(_: CurrentAdmin, session: SessionDep):
    """Every invitation ever issued, newest first — open, accepted, and revoked alike."""
    return await accounts.list_invitations(session)


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationRead)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    admin: CurrentAdmin,
    session: SessionDep,
    provider: IdentityProviderDep,
):
    """Close an open invitation, at the provider too when one is configured and will answer.

    Unlike creating one, revoking never requires a provider. Guru's own row is what admits
    people — see ``app.services.accounts.revoke_invitation`` — so refusing to revoke just
    because no provider is configured (or it will not answer) would leave an administrator
    unable to stop an enrollment they can see, which is the worse failure.
    """
    try:
        return await accounts.revoke_invitation(
            session, provider, actor=admin, invitation_id=invitation_id
        )
    except accounts.NoSuchInvitation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such invitation") from None
    except accounts.NotOpen:
        raise HTTPException(status.HTTP_409_CONFLICT, "that invitation is not open") from None
