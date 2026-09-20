"""Register, sign in, sign out, and see who you are (S21).

The token is returned to a browser as an httpOnly cookie and never as a response body: a
token JavaScript can read is a token an injected script can take, and the frontend has no
reason to hold it. Non-browser clients send the same token as ``Authorization: Bearer``, so
there is one credential and one table behind both.
"""

from datetime import timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import (
    CurrentLearner,
    IdentityProviderDep,
    SessionDep,
    SettingsDep,
    session_token_from,
)
from app.core.config import Settings
from app.core.identity import InvalidToken, ProviderError
from app.models.learner import Learner
from app.schemas.auth import (
    DevLoginRequest,
    LearnerRead,
    SessionListRead,
    SessionRead,
)
from app.services import auth as svc
from app.services import identity

router = APIRouter(prefix="/auth", tags=["auth"])

log = structlog.get_logger(__name__)

DEV_LEARNER_HANDLE = "dev"

# There is one sign-in door now, so this no longer has a second door to agree with — but the
# wording is the learner's, and it stays theirs: a refusal that says which of their accounts is
# in what state is the only thing they can act on.
_SUSPENDED = "This account is suspended. An administrator can reinstate it."


def _client_of(request: Request) -> str:
    """Who is asking, for throttling. The socket peer, not a header.

    ``X-Forwarded-For`` is attacker-controlled unless a proxy is known to rewrite it, and a
    throttle keyed on a value the attacker chooses is a throttle they opt out of. A deployment
    behind a trusted proxy has to say so before this can honour it — which it cannot yet, and
    that limit belongs in the open with the rest of them rather than papered over.
    """
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    # Same attributes as when it was set: a cookie is deleted by matching name, path and
    # domain, and a mismatch leaves the browser holding a cookie the server has already
    # revoked — harmless here, confusing for anybody reading it.
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain,
        path="/",
    )


@router.post("/exchange", response_model=LearnerRead)
async def exchange(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    provider: IdentityProviderDep,
    authorization: Annotated[str | None, Header()] = None,
):
    """Trade a proven identity for a Guru session (S21).

    The provider's token arrives in ``Authorization`` and is spent here, once. What the browser
    keeps is the same httpOnly cookie every other route already takes — which is why nothing
    downstream of this line knows Clerk exists, and why signing out, "log out everywhere" and
    suspension keep working exactly as they did.
    """
    if provider is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "sign-in is not configured on this server"
        )
    # Declared as a parameter rather than read off the raw request so the OpenAPI document
    # says this endpoint takes it — which is what lets the generated client send it instead of
    # every caller hand-rolling a fetch around the typed one (S21).
    header = authorization or ""
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")

    try:
        subject = await provider.verify(token)
        learner = await identity.enroll(session, provider, subject)
    except InvalidToken as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated") from exc
    except identity.NotInvited as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Guru is invite-only. Ask an administrator for an invitation.",
        ) from exc
    except identity.AccountSuspended as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _SUSPENDED) from exc
    except identity.AmbiguousIdentity as exc:
        log.warning("identity.ambiguous", subject=subject, learners=str(exc))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This sign-in matches more than one Guru account; an administrator has to resolve it.",
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "the sign-in provider could not be reached; try again",
        ) from exc

    issued = await svc.issue(session, learner, ttl=timedelta(hours=settings.session_ttl_hours))
    _set_session_cookie(response, issued.token, settings)
    return learner


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: SessionDep, settings: SettingsDep):
    """End the session this request is using.

    Deliberately not requiring a valid session: signing out with a token that has already
    expired must not be an error, or the client is left with a cookie it cannot clear.
    """
    token = session_token_from(request, settings)
    if token:
        await svc.revoke(session, token)
    _clear_session_cookie(response, settings)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response, learner: CurrentLearner, session: SessionDep, settings: SettingsDep
):
    """End every session for this learner, on every device."""
    await svc.revoke_all(session, learner.id)
    _clear_session_cookie(response, settings)


@router.get("/me", response_model=LearnerRead)
async def me(learner: CurrentLearner):
    """The learner this request is authenticated as."""
    return learner


@router.get("/sessions", response_model=SessionListRead)
async def sessions(
    request: Request, learner: CurrentLearner, session: SessionDep, settings: SettingsDep
):
    """Live sessions for this learner, so a forgotten one can be found and ended."""
    from app.core.security import token_fingerprint

    token = session_token_from(request, settings)
    current = token_fingerprint(token) if token else None
    rows = await svc.sessions_for(session, learner.id)
    return SessionListRead(
        sessions=[
            SessionRead(
                id=row.id,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                expires_at=row.expires_at,
                current=row.token_hash == current,
            )
            for row in rows
        ]
    )


@router.post("/dev-login", response_model=LearnerRead)
async def dev_login(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    body: DevLoginRequest | None = None,
):
    """Sign in as the development learner, with no credential (S21).

    The last surviving piece of the stub seam, kept so `poe dev` and the frontend still work
    with an empty database and nobody registered. It is an endpoint rather than a fallback
    inside the resolver on purpose: a fallback is invisible, appears in no schema, and is the
    exact shape of an auth boundary that looks present and is not. This one is listed in the
    OpenAPI document, refuses to exist unless ``GURU_DEV_AUTO_LOGIN`` is on, and production
    refuses to *start* while it is (``app.core.release``).

    It is also how the browser journeys sign in. They used to register through the password
    form; Clerk owns that form now, and its hosted UI cannot be driven in a CI browser with no
    network. Passing an address signs in as that account, creating it if needed, so each run
    gets a fresh one — the same door, opened by the same switch, with no second mechanism to
    keep safe.
    """
    if not settings.dev_auto_login:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    if body is not None and body.email is not None:
        address = svc.normalise_email(str(body.email))
        learner = await session.scalar(select(Learner).where(Learner.email == address))
        if learner is None:
            learner = Learner(
                handle=await svc.unique_handle(session, svc.handle_for(address)),
                email=address,
            )
            session.add(learner)
            await session.commit()
            await session.refresh(learner)
    else:
        learner = await session.scalar(select(Learner).where(Learner.handle == DEV_LEARNER_HANDLE))
        if learner is None:
            learner = Learner(handle=DEV_LEARNER_HANDLE, display_name="Dev Learner")
            session.add(learner)
            await session.commit()
            await session.refresh(learner)

    issued = await svc.issue(session, learner, ttl=timedelta(hours=settings.session_ttl_hours))
    _set_session_cookie(response, issued.token, settings)
    return learner
