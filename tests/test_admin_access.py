"""Who may read the operational endpoints, and who may not (P10).

Every authenticated learner used to be the same authenticated learner (S21 left the tier
explicitly undone), and `/ops/*` was open to anybody who knew the path — so this deployment's
bill, its queue depth and the list of what was currently broken were a public fact. The tests
here are about the boundary rather than the numbers behind it: the numbers have their own
suite in `test_ops_signals.py`.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings
from app.core.config import Settings, get_settings
from app.main import app
from app.models.learner import Learner
from tests.conftest import sign_in

API = "/api/v1"

GUARDED = [
    f"{API}/ops/ingestion",
    f"{API}/ops/spend",
    f"{API}/ops/alerts",
    f"{API}/ops/alerts/history",
]


@pytest.mark.parametrize("path", GUARDED)
async def test_an_operational_read_refuses_an_anonymous_request(
    anon_client: AsyncClient, path: str
) -> None:
    r = await anon_client.get(path)
    assert r.status_code == 401, f"{path} answered {r.status_code}"
    assert r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("path", GUARDED)
async def test_an_operational_read_refuses_an_ordinary_learner(
    api_client: AsyncClient, path: str
) -> None:
    """403, not 401: they are authenticated, they are simply not this.

    The distinction matters to the person on the other end — 401 sends somebody hunting for a
    broken session they do not have a problem with.
    """
    r = await api_client.get(path)
    assert r.status_code == 403, f"{path} answered {r.status_code}"


@pytest.mark.parametrize("path", GUARDED)
async def test_an_administrator_may_read_it(admin_client: AsyncClient, path: str) -> None:
    r = await admin_client.get(path)
    assert r.status_code == 200, f"{path} answered {r.status_code}: {r.text}"


async def test_the_liveness_and_readiness_probes_stay_open(anon_client: AsyncClient) -> None:
    """An orchestrator holds no credential, and a readiness probe that can fail on
    authentication takes healthy instances out of rotation for the wrong reason (S60)."""
    assert (await anon_client.get(f"{API}/ready")).status_code in (200, 503)
    assert (await anon_client.get("/health")).status_code == 200


# --- the monitor's credential ----------------------------------------------------------------


def _with_ops_token(token: str | None) -> None:
    settings = get_settings().model_copy(update={"ops_token": token})
    app.dependency_overrides[get_app_settings] = lambda: settings


@pytest.fixture(autouse=True)
def _restore_settings_override():
    yield
    app.dependency_overrides.pop(get_app_settings, None)


async def test_the_ops_token_opens_it_without_a_session(anon_client: AsyncClient) -> None:
    """A monitor polls every minute and should not hold a credential that expires — and the
    database it would have to be resolved against is one of the things being diagnosed."""
    _with_ops_token("s3cret-ops-token")
    r = await anon_client.get(f"{API}/ops/spend", headers={"X-Ops-Token": "s3cret-ops-token"})
    assert r.status_code == 200


async def test_a_wrong_ops_token_is_refused_like_no_token_at_all(
    anon_client: AsyncClient,
) -> None:
    _with_ops_token("s3cret-ops-token")
    r = await anon_client.get(f"{API}/ops/spend", headers={"X-Ops-Token": "not-the-token"})
    assert r.status_code == 401


async def test_an_ops_token_header_is_ignored_when_none_is_configured(
    anon_client: AsyncClient,
) -> None:
    """Unset must not mean "any token will do" — the comparison never runs at all."""
    _with_ops_token(None)
    r = await anon_client.get(f"{API}/ops/spend", headers={"X-Ops-Token": ""})
    assert r.status_code == 401
    r = await anon_client.get(f"{API}/ops/spend", headers={"X-Ops-Token": "anything"})
    assert r.status_code == 401


# --- the grant itself ------------------------------------------------------------------------


async def test_a_new_learner_is_not_an_administrator(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The column's default is the security decision: a backfill that guessed would guess up."""
    learner = Learner(handle=f"plain-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    assert learner.is_admin is False

    await sign_in(anon_client, db_session, learner)
    assert (await anon_client.get(f"{API}/auth/me")).json()["is_admin"] is False


async def test_the_app_is_told_who_is_an_administrator(admin_client: AsyncClient) -> None:
    """So it can decline to show a door that would answer 403."""
    assert (await admin_client.get(f"{API}/auth/me")).json()["is_admin"] is True


def test_production_refuses_to_start_with_no_ops_token() -> None:
    """Otherwise `/ops/*` admits only a session, and a session is a database read (P10)."""
    from app.core.release import production_problems

    problems = production_problems(Settings(ops_token=None))
    assert any("GURU_OPS_TOKEN" in p for p in problems)
    assert not any("GURU_OPS_TOKEN" in p for p in production_problems(Settings(ops_token="t")))


# --- the bootstrap command -----------------------------------------------------------------


async def test_the_grant_command_promotes_demotes_and_refuses_an_unknown_address(
    engine, monkeypatch
) -> None:
    """The only way a deployment gets its first administrator, and nothing drove it.

    Committing for real on its own connection, because that is what the command does — it runs
    outside any request, against the live database, which is the whole reason it exists.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.workers import grant_admin

    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def make():
        async with factory() as session:
            yield session

    monkeypatch.setattr(grant_admin, "SessionFactory", make)

    handle = f"grant-{uuid.uuid4().hex[:8]}"
    email = f"{handle}@example.com"
    async with factory() as setup:
        setup.add(Learner(handle=handle, email=email))
        await setup.commit()

    try:
        assert await grant_admin.run(email, revoke=False) == 0
        async with factory() as check:
            granted = await check.scalar(select(Learner).where(Learner.email == email))
            assert granted is not None and granted.is_admin is True

        # Mixed case, because an operator types an address the way a person writes one and the
        # column is stored normalised.
        assert await grant_admin.run(email.upper(), revoke=True) == 0
        async with factory() as check:
            demoted = await check.scalar(select(Learner).where(Learner.email == email))
            assert demoted is not None and demoted.is_admin is False

        # Non-zero, so a deployment script that typos an address stops rather than reporting a
        # grant that never happened.
        assert await grant_admin.run("nobody@example.com", revoke=False) == 1
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(Learner).where(Learner.email == email))
            await cleanup.commit()
