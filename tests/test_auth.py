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
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import DEV_LEARNER_HANDLE
from app.core.config import Settings, get_settings
from app.core.security import hash_password, token_fingerprint, verify_password
from app.models.auth import LearnerSession
from app.models.learner import Learner
from app.services import auth as svc
from tests.conftest import sign_in

API = "/api/v1"
PASSWORD = "a sufficiently long password"


def _cookie(response) -> str | None:
    return response.cookies.get(get_settings().session_cookie_name)


# --- registration ----------------------------------------------------------------------------


async def test_registering_creates_an_account_and_signs_it_in(anon_client: AsyncClient) -> None:
    r = await anon_client.post(
        f"{API}/auth/register",
        json={"email": "ada@example.com", "password": PASSWORD, "display_name": "Ada"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "ada@example.com"
    assert _cookie(r), "registration should leave the client signed in"

    me = await anon_client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "Ada"


async def test_the_session_cookie_is_not_readable_by_script(anon_client: AsyncClient) -> None:
    """A token JavaScript can read is a token an injected script can take."""
    r = await anon_client.post(
        f"{API}/auth/register", json={"email": "http@example.com", "password": PASSWORD}
    )
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "path=/" in header


async def test_no_response_ever_carries_the_token_in_its_body(anon_client: AsyncClient) -> None:
    r = await anon_client.post(
        f"{API}/auth/register", json={"email": "body@example.com", "password": PASSWORD}
    )
    token = _cookie(r)
    assert token
    assert token not in r.text
    me = await anon_client.get(f"{API}/auth/me")
    assert token not in me.text
    assert "password" not in me.text


async def test_an_address_can_only_be_registered_once(anon_client: AsyncClient) -> None:
    first = await anon_client.post(
        f"{API}/auth/register", json={"email": "twice@example.com", "password": PASSWORD}
    )
    assert first.status_code == 201
    second = await anon_client.post(
        f"{API}/auth/register", json={"email": "twice@example.com", "password": PASSWORD}
    )
    assert second.status_code == 409


async def test_case_does_not_make_a_second_account(anon_client: AsyncClient) -> None:
    """``Ada@`` and ``ada@`` are one account, because treating them as two locks people out."""
    await anon_client.post(
        f"{API}/auth/register", json={"email": "Mixed@Example.COM", "password": PASSWORD}
    )
    again = await anon_client.post(
        f"{API}/auth/register", json={"email": "mixed@example.com", "password": PASSWORD}
    )
    assert again.status_code == 409


async def test_a_short_password_is_refused_before_it_reaches_the_database(
    anon_client: AsyncClient,
) -> None:
    r = await anon_client.post(
        f"{API}/auth/register", json={"email": "short@example.com", "password": "short"}
    )
    assert r.status_code == 422


async def test_a_malformed_address_is_refused(anon_client: AsyncClient) -> None:
    r = await anon_client.post(
        f"{API}/auth/register", json={"email": "not-an-address", "password": PASSWORD}
    )
    assert r.status_code == 422


async def test_the_stored_password_is_a_hash_and_not_the_password(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    await anon_client.post(
        f"{API}/auth/register", json={"email": "hashed@example.com", "password": PASSWORD}
    )
    learner = await db_session.scalar(select(Learner).where(Learner.email == "hashed@example.com"))
    assert learner is not None
    assert learner.password_hash is not None
    assert PASSWORD not in learner.password_hash
    assert learner.password_hash.startswith("$argon2")
    assert verify_password(PASSWORD, learner.password_hash)


async def test_a_password_without_an_address_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    """The one credential combination that cannot be signed in with or recovered from."""
    await db_session.execute(text("SAVEPOINT ck_test"))
    db_session.add(
        Learner(
            handle=f"ck-{uuid.uuid4().hex[:8]}", email=None, password_hash=hash_password(PASSWORD)
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --- signing in ------------------------------------------------------------------------------


async def test_login_with_the_right_password_starts_a_session(anon_client: AsyncClient) -> None:
    await anon_client.post(
        f"{API}/auth/register", json={"email": "login@example.com", "password": PASSWORD}
    )
    await anon_client.post(f"{API}/auth/logout")

    r = await anon_client.post(
        f"{API}/auth/login", json={"email": "login@example.com", "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 200


async def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(
    anon_client: AsyncClient,
) -> None:
    """Telling them apart is a free membership list for anybody who asks."""
    await anon_client.post(
        f"{API}/auth/register", json={"email": "known@example.com", "password": PASSWORD}
    )
    wrong = await anon_client.post(
        f"{API}/auth/login", json={"email": "known@example.com", "password": "the wrong password"}
    )
    unknown = await anon_client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


async def test_a_learner_with_no_password_cannot_be_signed_in_as(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Every learner that predates S21 is in this state, and none of them is a way in."""
    db_session.add(
        Learner(
            handle=f"old-{uuid.uuid4().hex[:8]}", email="legacy@example.com", password_hash=None
        )
    )
    await db_session.flush()
    r = await anon_client.post(
        f"{API}/auth/login", json={"email": "legacy@example.com", "password": PASSWORD}
    )
    assert r.status_code == 401


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


async def test_the_operational_endpoints_stay_open(anon_client: AsyncClient) -> None:
    """Read by an orchestrator and a monitor, neither of which holds a learner session (S60)."""
    assert (await anon_client.get(f"{API}/ready")).status_code in (200, 503)
    assert (await anon_client.get(f"{API}/ops/ingestion")).status_code == 200
    assert (await anon_client.get("/health")).status_code == 200


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


async def test_dev_login_does_not_exist_when_it_is_turned_off(
    anon_client: AsyncClient, settings_without_dev_login: None
) -> None:
    r = await anon_client.post(f"{API}/auth/dev-login")
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
