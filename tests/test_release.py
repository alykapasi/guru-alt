"""Refusing to start, readiness, and the queue signal an operator alerts on (S60)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppEnv, Settings
from app.core.readiness import PROBE_TIMEOUT_SECONDS, check_blob_store, check_database, readiness
from app.core.release import (
    MisconfiguredForProduction,
    enforce_production_settings,
    production_problems,
)
from app.models.learner import Learner
from app.models.source import Source, SourceKind, SourceStatus
from app.services.ingestion import backlog
from app.storage.base import BlobNotFound
from app.storage.memory import InMemoryBlobStore


def _now() -> datetime:
    """`sources` timestamps are TIMESTAMP WITHOUT TIME ZONE, so comparisons need naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


def _prod(**overrides: object) -> Settings:
    """A correctly configured production deployment, before each test spoils one thing."""
    base = Settings(
        env=AppEnv.PROD,
        debug=False,
        database_url="postgresql+asyncpg://guru:s3cret@db.internal:5432/guru",
        redis_url="redis://cache.internal:6379/0",
        blob_endpoint_url="https://s3.eu-west-2.amazonaws.com",
        blob_access_key="AKIAREAL",
        blob_secret_key="realsecret",
        cors_origins=["https://app.guru.example"],
        anthropic_api_key="sk-ant-real",
        dev_auto_login=False,
        session_cookie_secure=True,
    )
    return base.model_copy(update=overrides) if overrides else base


def test_a_correct_production_config_starts() -> None:
    enforce_production_settings(_prod())


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"debug": True}, "GURU_DEBUG"),
        (
            {"database_url": "postgresql+asyncpg://guru:guru@db.internal:5432/guru"},
            "docker-compose credentials",
        ),
        (
            {"database_url": "postgresql+asyncpg://u:p@localhost:5433/guru"},
            "GURU_DATABASE_URL points at localhost",
        ),
        ({"blob_secret_key": "minioadmin"}, "MinIO defaults"),
        ({"blob_endpoint_url": "http://localhost:9000"}, "GURU_BLOB_ENDPOINT_URL"),
        ({"redis_url": "redis://localhost:6379/0"}, "GURU_REDIS_URL"),
        ({"cors_origins": ["*"]}, "every origin"),
        ({"cors_origins": ["http://localhost:5173"]}, "localhost dev server"),
        ({"anthropic_api_key": "", "openrouter_api_key": ""}, "no model provider key"),
        # S21: a session issued with no credential, for anybody who finds the endpoint.
        ({"dev_auto_login": True}, "GURU_DEV_AUTO_LOGIN"),
        ({"session_cookie_secure": False}, "GURU_SESSION_COOKIE_SECURE"),
        (
            {"session_cookie_samesite": "none", "session_cookie_secure": False},
            "GURU_SESSION_COOKIE_SAMESITE=none",
        ),
    ],
)
def test_each_development_default_refuses_production(overrides: dict, expected: str) -> None:
    with pytest.raises(MisconfiguredForProduction) as caught:
        enforce_production_settings(_prod(**overrides))
    assert any(expected in problem for problem in caught.value.problems)


def test_every_problem_is_reported_at_once() -> None:
    """One restart per discovered problem is one outage per discovered problem."""
    problems = production_problems(
        _prod(debug=True, cors_origins=["*"], blob_secret_key="minioadmin")
    )
    assert len(problems) >= 3


def test_dev_defaults_are_fine_outside_production() -> None:
    # The same settings that are refused as prod are exactly how a laptop is meant to run.
    enforce_production_settings(Settings(env=AppEnv.DEV))


async def test_database_probe_reports_reachability(db_session: AsyncSession) -> None:
    status = await check_database(db_session)
    assert status.ok and status.name == "database"


async def test_blob_probe_treats_a_missing_object_as_an_answer() -> None:
    store = InMemoryBlobStore()
    status = await check_blob_store(store)
    assert status.ok, "not-found is a successful round trip, not a failure"


async def test_a_probe_reports_a_failure_instead_of_raising(db_session: AsyncSession) -> None:
    class Broken(InMemoryBlobStore):
        async def get(self, key: str) -> bytes:
            raise ConnectionRefusedError("s3 unreachable")

    report = await readiness(db_session, Broken())
    assert not report.ready
    blob = next(d for d in report.dependencies if d.name == "blob_store")
    assert blob.detail == "ConnectionRefusedError"
    # The DSN and any credentials in a driver's message must not reach an open endpoint.
    assert "unreachable" not in (blob.detail or "")


async def test_a_hanging_dependency_times_out_rather_than_hanging(
    db_session: AsyncSession,
) -> None:
    import asyncio

    class Hanging(InMemoryBlobStore):
        async def get(self, key: str) -> bytes:
            await asyncio.sleep(PROBE_TIMEOUT_SECONDS * 10)
            raise BlobNotFound(key)

    report = await asyncio.wait_for(
        readiness(db_session, Hanging()), timeout=PROBE_TIMEOUT_SECONDS * 3
    )
    assert not report.ready
    blob = next(d for d in report.dependencies if d.name == "blob_store")
    assert "did not answer" in (blob.detail or "")


async def _source(session: AsyncSession, learner: Learner, **kwargs) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"f-{uuid.uuid4().hex[:6]}.pdf",
        **kwargs,
    )
    session.add(source)
    await session.flush()
    return source


async def test_backlog_sees_a_queue_that_has_stopped_draining(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"ops-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    old = _now() - timedelta(hours=4)
    waiting = await _source(db_session, learner, status=SourceStatus.PENDING)
    waiting.created_at = old
    await _source(db_session, learner, status=SourceStatus.PENDING)
    await db_session.flush()

    report = await backlog(db_session)

    assert report.pending == 2
    assert report.processing == 0
    assert report.stalled, "work waiting with nothing in flight is a dead consumer"
    assert report.oldest_pending_age_seconds is not None
    assert report.oldest_pending_age_seconds > 3 * 3600


async def test_backlog_separates_saturated_from_stalled(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"ops-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    await _source(db_session, learner, status=SourceStatus.PENDING)
    await _source(db_session, learner, status=SourceStatus.PROCESSING)

    report = await backlog(db_session)

    assert report.pending == 1 and report.processing == 1
    assert not report.stalled
    assert report.max_concurrent_jobs > 0


async def test_backlog_counts_leases_nobody_swept_up(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"ops-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    stale = await _source(db_session, learner, status=SourceStatus.PROCESSING)
    stale.lease_expires_at = _now() - timedelta(hours=1)
    live = await _source(db_session, learner, status=SourceStatus.PROCESSING)
    live.lease_expires_at = _now() + timedelta(hours=1)
    await db_session.flush()

    report = await backlog(db_session)

    assert report.expired_leases == 1


async def test_ready_is_503_when_a_dependency_is_down(api_client: AsyncClient) -> None:
    from app.api.deps import get_blob_store
    from app.main import app

    class Broken(InMemoryBlobStore):
        async def get(self, key: str) -> bytes:
            raise ConnectionRefusedError

    app.dependency_overrides[get_blob_store] = lambda: Broken()
    try:
        response = await api_client.get("/api/v1/ready")
    finally:
        app.dependency_overrides.pop(get_blob_store, None)

    assert response.status_code == 503
    assert response.json()["ready"] is False


async def test_health_stays_trivial_while_readiness_fails(api_client: AsyncClient) -> None:
    from app.api.deps import get_blob_store
    from app.main import app

    class Broken(InMemoryBlobStore):
        async def get(self, key: str) -> bytes:
            raise ConnectionRefusedError

    app.dependency_overrides[get_blob_store] = lambda: Broken()
    try:
        assert (await api_client.get("/health")).status_code == 200
        assert (await api_client.get("/api/v1/ready")).status_code == 503
    finally:
        app.dependency_overrides.pop(get_blob_store, None)
