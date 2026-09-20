"""Real identity, and what it refuses (S21).

The stub resolved — and created — one dev learner for any caller, so every route had a
learner whether or not anybody had proved anything. These tests are the difference: a
credential that is checked, a session that can be withdrawn, and a request without one that
gets nowhere.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import DEV_LEARNER_HANDLE
from app.core.config import Settings, get_settings
from app.core.security import token_fingerprint
from app.main import app
from app.models.auth import LearnerSession
from app.models.learner import Learner
from app.services import auth as svc
from tests.conftest import sign_in

API = "/api/v1"


def _cookie(response) -> str | None:
    return response.cookies.get(get_settings().session_cookie_name)


# --- the password system is gone, not disabled -------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/auth/register", {"email": "a@example.com", "password": "x" * 12}),
        ("/auth/login", {"email": "a@example.com", "password": "x" * 12}),
        ("/auth/password", {"current_password": "x" * 12, "new_password": "y" * 12}),
        ("/auth/email", {"email": "b@example.com", "password": "x" * 12}),
        ("/auth/password-reset", {"email": "a@example.com"}),
        ("/auth/password-reset/confirm", {"token": "t", "new_password": "y" * 12}),
    ],
)
async def test_the_password_routes_are_gone(
    anon_client: AsyncClient, path: str, body: dict
) -> None:
    """Gone, not disabled. An endpoint that answers 401 is an endpoint somebody can attack,
    and a disabled one still has to be kept correct forever. Clerk owns credentials now."""
    assert (await anon_client.post(f"{API}{path}", json=body)).status_code == 404


def test_the_openapi_document_offers_no_password_route() -> None:
    """The contract is the other half of removal: a client generated from this document must
    not be able to name a password route at all."""
    paths = app.openapi()["paths"]

    assert not [p for p in paths if "password" in p or p.endswith("/auth/register")]


def test_no_password_machinery_survives_in_the_service() -> None:
    """The routes going is not the same as the code going.

    Dead credential-checking code is worse than live code: nothing exercises it, so nothing
    notices when it rots, and the next person to need "just a quick login" finds it waiting.
    """
    for gone in (
        "register",
        "authenticate",
        "check_sign_in_allowed",
        "record_failed_sign_in",
        "purge_sign_in_attempts",
        "begin_password_reset",
        "complete_password_reset",
        "purge_password_resets",
        "change_password",
        "change_email",
    ):
        assert not hasattr(svc, gone), f"app.services.auth.{gone} still exists"

    import app.core.security as security

    assert not hasattr(security, "hash_password")
    assert not hasattr(security, "verify_password")


def test_a_learner_no_longer_carries_a_password_at_all() -> None:
    """The column is dropped, so there is nothing left to leak, reset, or forget to hash."""
    assert not hasattr(Learner, "password_hash")


# --- what an unauthenticated request gets ----------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{API}/auth/me"),
        ("GET", f"{API}/conversations"),
        ("POST", f"{API}/conversations"),
        ("GET", f"{API}/profile"),
        ("GET", f"{API}/sources"),
        ("GET", f"{API}/topics/{uuid.uuid4()}/note"),
        ("GET", f"{API}/activity"),
        ("GET", f"{API}/subjects/{uuid.uuid4()}/lesson-plan"),
        ("GET", f"{API}/memory"),
        ("GET", f"{API}/me/export"),
        ("GET", f"{API}/me/retention"),
    ],
)
async def test_a_learner_route_refuses_a_request_with_no_session(
    anon_client: AsyncClient, method: str, path: str
) -> None:
    r = await anon_client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 401, f"{method} {path} answered {r.status_code}"
    assert r.headers.get("www-authenticate") == "Bearer"


async def test_the_probes_stay_open_and_the_operational_reads_do_not(
    anon_client: AsyncClient,
) -> None:
    """Liveness and readiness are read by an orchestrator that holds no credential (S60).

    Everything under `/ops` used to be open on the same reasoning, which conflated "polled by
    a machine" with "safe for anybody" — the queue depth, the bill and the list of what is
    broken are operator facts. They take an administrator or the ops token now (P10); the
    boundary has its own suite in `test_admin_access.py`.
    """
    assert (await anon_client.get(f"{API}/ready")).status_code in (200, 503)
    assert (await anon_client.get("/health")).status_code == 200
    assert (await anon_client.get(f"{API}/ops/ingestion")).status_code == 401


async def test_a_made_up_token_is_refused(anon_client: AsyncClient) -> None:
    anon_client.cookies.set(get_settings().session_cookie_name, "not-a-real-token")
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


# --- sessions are checked on use, not only issued --------------------------------------------


async def test_an_expired_session_stops_working(
    anon_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    issued = await svc.issue(db_session, api_learner, ttl=timedelta(seconds=-1), commit=False)
    anon_client.cookies.set(get_settings().session_cookie_name, issued.token)
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_logging_out_revokes_the_session_immediately(api_client: AsyncClient) -> None:
    assert (await api_client.get(f"{API}/auth/me")).status_code == 200
    assert (await api_client.post(f"{API}/auth/logout")).status_code == 204
    assert (await api_client.get(f"{API}/auth/me")).status_code == 401


async def test_logging_out_with_a_dead_session_is_not_an_error(api_client: AsyncClient) -> None:
    """Otherwise the client is left holding a cookie it cannot clear."""
    await api_client.post(f"{API}/auth/logout")
    assert (await api_client.post(f"{API}/auth/logout")).status_code == 204


async def test_logout_all_ends_every_session_on_every_device(
    api_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    await sign_in(anon_client, db_session, api_learner)
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 200

    assert (await api_client.post(f"{API}/auth/logout-all")).status_code == 204
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401
    assert (await api_client.get(f"{API}/auth/me")).status_code == 401


async def test_deleting_the_account_stops_its_sessions_authenticating(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """A session that outlived its owner would be a live credential for a gone account."""
    await db_session.delete(api_learner)
    await db_session.flush()
    assert (await api_client.get(f"{API}/auth/me")).status_code == 401
    assert await db_session.scalar(select(LearnerSession).limit(1)) is None


async def test_the_session_list_marks_the_one_being_used(
    api_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    await sign_in(anon_client, db_session, api_learner)
    r = await api_client.get(f"{API}/auth/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 2
    assert [s["current"] for s in sessions].count(True) == 1


# --- the token, as a bearer header -----------------------------------------------------------


async def test_the_same_token_works_as_a_bearer_header(
    anon_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    issued = await svc.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)
    r = await anon_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {issued.token}"})
    assert r.status_code == 200
    assert r.json()["id"] == str(api_learner.id)


async def test_an_explicit_bearer_header_beats_the_cookie(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sending the header is a deliberate act; the cookie is attached by the browser."""
    other = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    issued = await svc.issue(db_session, other, ttl=timedelta(hours=1), commit=False)

    r = await api_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {issued.token}"})
    assert r.json()["id"] == str(other.id)


# --- what is stored --------------------------------------------------------------------------


async def test_the_token_itself_is_never_stored(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """A dump, a backup or a log line must not be replayable as a login."""
    issued = await svc.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)
    row = await db_session.scalar(
        select(LearnerSession).where(LearnerSession.id == issued.session_id)
    )
    assert row is not None
    assert row.token_hash != issued.token
    assert row.token_hash == token_fingerprint(issued.token)


async def test_purging_removes_dead_sessions_and_keeps_a_recently_revoked_one(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """While a revoked row exists, "your session ended" is distinguishable from silence."""
    expired = await svc.issue(db_session, api_learner, ttl=timedelta(seconds=-1), commit=False)
    live = await svc.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)
    revoked = await svc.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)
    await svc.revoke(db_session, revoked.token)

    removed = await svc.purge_expired(db_session, keep_revoked_for=timedelta(days=7))
    assert removed == 1

    remaining = {row.id for row in (await db_session.scalars(select(LearnerSession))).all()}
    assert expired.session_id not in remaining
    assert live.session_id in remaining
    assert revoked.session_id in remaining


async def test_a_long_revoked_session_is_purged(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    issued = await svc.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)
    await svc.revoke(db_session, issued.token)
    row = await db_session.get(LearnerSession, issued.session_id)
    assert row is not None
    row.revoked_at = datetime.now(UTC) - timedelta(days=30)
    await db_session.flush()

    assert await svc.purge_expired(db_session, keep_revoked_for=timedelta(days=7)) == 1


# --- the development sign-in seam --------------------------------------------------------------


async def test_dev_login_signs_in_as_the_development_learner(anon_client: AsyncClient) -> None:
    r = await anon_client.post(f"{API}/auth/dev-login")
    assert r.status_code == 200, r.text
    assert r.json()["handle"] == DEV_LEARNER_HANDLE
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 200


async def test_dev_login_can_name_the_account_it_signs_in_as(anon_client: AsyncClient) -> None:
    """The journeys need a fresh account per run, and the password form is going away.

    Twice with the same address, because a journey re-run must land on the same account rather
    than pile up a new one each time.
    """
    r = await anon_client.post(f"{API}/auth/dev-login", json={"email": "journey-1@example.com"})

    assert r.status_code == 200, r.text
    assert r.json()["email"] == "journey-1@example.com"
    assert r.json()["handle"] != DEV_LEARNER_HANDLE
    assert (await anon_client.get(f"{API}/auth/me")).json()["email"] == "journey-1@example.com"

    again = await anon_client.post(f"{API}/auth/dev-login", json={"email": "journey-1@example.com"})

    assert again.json()["id"] == r.json()["id"]


async def test_dev_login_normalises_the_address_it_is_given(anon_client: AsyncClient) -> None:
    """Same account whichever way it is spelled, so a journey cannot fork one into two."""
    first = await anon_client.post(f"{API}/auth/dev-login", json={"email": "Mixed@Example.com"})
    second = await anon_client.post(f"{API}/auth/dev-login", json={"email": "mixed@example.com"})

    assert first.json()["email"] == "mixed@example.com"
    assert second.json()["id"] == first.json()["id"]


async def test_dev_login_does_not_exist_when_it_is_turned_off(
    anon_client: AsyncClient, settings_without_dev_login: None
) -> None:
    r = await anon_client.post(f"{API}/auth/dev-login")
    assert r.status_code == 404
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_dev_login_stays_gone_when_turned_off_even_with_an_address(
    anon_client: AsyncClient, settings_without_dev_login: None
) -> None:
    """The body must not be a second way in: the switch is the whole boundary."""
    r = await anon_client.post(f"{API}/auth/dev-login", json={"email": "journey-2@example.com"})

    assert r.status_code == 404
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


@pytest.fixture
def settings_without_dev_login():
    from app.api.deps import get_app_settings
    from app.main import app

    previous = app.dependency_overrides.get(get_app_settings)
    app.dependency_overrides[get_app_settings] = lambda: Settings(dev_auto_login=False)
    yield
    if previous is None:
        app.dependency_overrides.pop(get_app_settings, None)
    else:
        app.dependency_overrides[get_app_settings] = previous


# --- the sweep that keeps the table bounded ---------------------------------------------------


async def test_the_worker_sweep_deletes_dead_sessions(
    db_session: AsyncSession, api_learner: Learner, monkeypatch
) -> None:
    """An auth table nobody prunes grows for the life of the deployment."""
    import contextlib

    from app.workers import tasks

    await svc.issue(db_session, api_learner, ttl=timedelta(seconds=-1), commit=False)
    live = await svc.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    await tasks._purge_sessions_once()

    remaining = [row.id for row in (await db_session.scalars(select(LearnerSession))).all()]
    assert remaining == [live.session_id]
