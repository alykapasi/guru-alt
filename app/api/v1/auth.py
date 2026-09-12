"""Register, sign in, sign out, and see who you are (S21).

The token is returned to a browser as an httpOnly cookie and never as a response body: a
token JavaScript can read is a token an injected script can take, and the frontend has no
reason to hold it. Non-browser clients send the same token as ``Authorization: Bearer``, so
there is one credential and one table behind both.
"""

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentLearner, SessionDep, SettingsDep, session_token_from
from app.core.config import Settings
from app.models.learner import Learner
from app.schemas.auth import (
    LearnerRead,
    LoginRequest,
    RegisterRequest,
    SessionListRead,
    SessionRead,
)
from app.services import auth as svc

router = APIRouter(prefix="/auth", tags=["auth"])

DEV_LEARNER_HANDLE = "dev"

# One message for both halves of a failed sign-in. Which half was wrong is a free membership
# list, and it is worth exactly nothing to somebody who typed their own password wrong.
_REJECTED = "email or password is incorrect"


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


@router.post("/register", response_model=LearnerRead, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, response: Response, session: SessionDep, settings: SettingsDep
):
    """Create an account and sign in as it."""
    try:
        learner = await svc.register(
            session,
            email=str(body.email),
            password=body.password,
            display_name=body.display_name,
        )
    except svc.EmailTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "that email is already registered") from exc

    issued = await svc.issue(session, learner, ttl=timedelta(hours=settings.session_ttl_hours))
    _set_session_cookie(response, issued.token, settings)
    return learner


@router.post("/login", response_model=LearnerRead)
async def login(body: LoginRequest, response: Response, session: SessionDep, settings: SettingsDep):
    """Exchange credentials for a session."""
    try:
        learner = await svc.authenticate(session, email=str(body.email), password=body.password)
    except svc.InvalidCredentials as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _REJECTED) from exc

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
async def dev_login(response: Response, session: SessionDep, settings: SettingsDep):
    """Sign in as the development learner, with no credential (S21).

    The last surviving piece of the stub seam, kept so `poe dev` and the frontend still work
    with an empty database and nobody registered. It is an endpoint rather than a fallback
    inside the resolver on purpose: a fallback is invisible, appears in no schema, and is the
    exact shape of an auth boundary that looks present and is not. This one is listed in the
    OpenAPI document, refuses to exist unless ``GURU_DEV_AUTO_LOGIN`` is on, and production
    refuses to *start* while it is (``app.core.release``).
    """
    if not settings.dev_auto_login:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    learner = await session.scalar(select(Learner).where(Learner.handle == DEV_LEARNER_HANDLE))
    if learner is None:
        learner = Learner(handle=DEV_LEARNER_HANDLE, display_name="Dev Learner")
        session.add(learner)
        await session.commit()
        await session.refresh(learner)

    issued = await svc.issue(session, learner, ttl=timedelta(hours=settings.session_ttl_hours))
    _set_session_cookie(response, issued.token, settings)
    return learner
