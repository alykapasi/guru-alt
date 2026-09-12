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
from app.core import mail
from app.core.config import Settings
from app.models.learner import Learner
from app.schemas.auth import (
    EmailChange,
    LearnerRead,
    LoginRequest,
    PasswordChange,
    PasswordResetConfirm,
    PasswordResetRequest,
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

# Deliberately not "no account with that address". A reset endpoint that distinguishes gives
# back through another door exactly the enumeration oracle sign-in refuses to open.
_RESET_SENT = "if that address has an account, a reset link is on its way"


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
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
):
    """Exchange credentials for a session."""
    client = _client_of(request)
    try:
        await svc.check_sign_in_allowed(
            session,
            email=str(body.email),
            client=client,
            window=timedelta(minutes=settings.sign_in_window_minutes),
            max_per_email=settings.sign_in_max_failures_per_email,
            max_per_client=settings.sign_in_max_failures_per_client,
        )
    except svc.TooManyAttempts as exc:
        # 429 rather than 401, because this one *is* worth distinguishing: it is the only way
        # a person locked out by somebody else's guessing can tell what is happening to them.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many sign-in attempts; try again later"
        ) from exc

    try:
        learner = await svc.authenticate(session, email=str(body.email), password=body.password)
    except svc.InvalidCredentials as exc:
        await svc.record_failed_sign_in(session, email=str(body.email), client=client)
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


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange,
    request: Request,
    learner: CurrentLearner,
    session: SessionDep,
    settings: SettingsDep,
):
    """Set a new password. Every *other* session ends; this one keeps working.

    Signing the learner out of the tab they are typing in would make the safe action annoying,
    and a password change is often a response to suspecting another device — so the other
    devices are what stop working.
    """
    try:
        await svc.change_password(
            session,
            learner,
            current=body.current_password,
            new=body.new_password,
            keep_token=session_token_from(request, settings),
        )
    except svc.WrongPassword as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is incorrect") from exc


@router.post("/email", response_model=LearnerRead)
async def change_email(body: EmailChange, learner: CurrentLearner, session: SessionDep):
    """Move to a new address, proving the password.

    No verification of the new address, which is the honest gap: until a message can be
    delivered (``app.core.mail``) there is no way to establish that the learner owns what they
    typed, and a typo here costs them the account.
    """
    try:
        await svc.change_email(session, learner, password=body.password, email=str(body.email))
    except svc.WrongPassword as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "password is incorrect") from exc
    except svc.EmailTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "that email is already registered") from exc
    return learner


@router.post("/password-reset", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    body: PasswordResetRequest, session: SessionDep, settings: SettingsDep
):
    """Start a reset. Answers the same whether or not the address has an account."""
    if not settings.password_reset_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    issued = await svc.begin_password_reset(
        session,
        email=str(body.email),
        ttl=timedelta(minutes=settings.password_reset_ttl_minutes),
    )
    if issued is not None:
        await mail.build_mailer().send(
            to=str(body.email),
            subject="Reset your Guru password",
            body=f"Use this code to set a new password: {issued.token}",
        )
    return {"detail": _RESET_SENT}


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    body: PasswordResetConfirm, session: SessionDep, settings: SettingsDep
):
    """Spend a reset token and set the new password. Every session ends.

    A reset is what somebody does when they think the account may not be theirs alone, so
    leaving the intruder's session working would make it a gesture.
    """
    if not settings.password_reset_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    learner = await svc.complete_password_reset(
        session, token=body.token, password=body.new_password
    )
    if learner is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that reset link is not usable")


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
