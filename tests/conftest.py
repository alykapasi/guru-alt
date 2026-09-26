"""Shared test fixtures.

Each test gets its own NullPool engine (created and disposed inside that test's event
loop, avoiding cross-loop pool reuse), and runs inside an outer transaction that is rolled
back at the end — so tests are isolated and leave the database clean. The session joins
that transaction with savepoints, so application `commit()` calls don't break isolation.

`api_client` drives the FastAPI app in-process (httpx ASGI transport) with `get_session`
overridden to share the test's transactional session. It is **authenticated**: it carries a
real session cookie for the `api_learner` fixture, issued through `app.services.auth`, so
every API test goes through the same resolver production uses (S21) rather than through a
test-only override of it. `anon_client` is the same client with no credential, for the tests
that assert what an unauthenticated request gets.

Cost accounting normally commits on its *own* connection (see `app.services.llm_log`), which
in a test would mean rows referencing learners this transaction has not committed, and — for
tests with no database at all — connections from the process-wide pool bound to a previous
test's event loop. So it is redirected for every test: `accounting_default` sends it nowhere,
and `db_session` upgrades it to a second session on the test's own connection, where foreign
keys resolve and the rows roll back with everything else. `test_llm_log.py` covers the real
independent-connection behaviour directly.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from app.api.deps import get_concept_link_judge_enqueuer, get_engine
from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.models.learner import Learner
from app.services import auth
from app.services.decisions import DecisionPolicy, DecisionRuntime, set_runtime
from app.services.llm_log import set_accounting_session_factory
from app.storage import InMemoryBlobStore


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


class _DiscardedAccounting:
    """Stands in for an accounting session in tests that have no database.

    Accounting is deliberately best-effort in production, so a test that never touches the
    database would otherwise reach the process-wide engine and its cross-loop connections.
    """

    def add(self, obj: object) -> None:
        pass

    async def commit(self) -> None:
        pass


@pytest.fixture(autouse=True)
def object_store_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the suite's default object store in-memory, so no test can reach MinIO.

    A test that resolves `get_blob_store` without overriding it built the real S3 client and
    reached MinIO on :9000. That *passes* on a developer's machine, where `docker compose` is
    up, and fails in CI, where the `test` job runs no object store — so the mistake is
    invisible exactly where it is made, and two tests shipped that way.

    Defaulting rather than refusing, because refusing is wrong for `/ready`: readiness probes
    the store on purpose and is allowed to answer 503, but the store arrives as a dependency,
    so raising at resolution time breaks the request before the handler can report anything.
    An in-memory store answers `BlobNotFound`, which is the probe's definition of "the store
    answered", and the endpoint behaves as it does in production.

    A fresh store per test, since `monkeypatch` is function-scoped — nothing leaks between
    tests. Fixtures that install their own store still win: they override the FastAPI
    dependency and never call this at all.
    """
    store = InMemoryBlobStore()
    monkeypatch.setattr("app.api.deps._blob_store", lambda: store)


@pytest.fixture(autouse=True)
def decisions_off() -> Iterator[None]:
    """Every test starts with every Jev question off and no client, whatever the developer's
    `.env` says — the suite must never reach the network or depend on a key (S78)."""
    previous = set_runtime(DecisionRuntime(client=None, policy=DecisionPolicy.off()))
    try:
        yield
    finally:
        set_runtime(previous)


@pytest_asyncio.fixture(autouse=True)
async def accounting_default() -> AsyncIterator[None]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        yield _DiscardedAccounting()

    previous = set_accounting_session_factory(factory)
    try:
        yield
    finally:
        set_accounting_session_factory(previous)


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine, accounting_default: None) -> AsyncIterator[AsyncSession]:
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        async with _accounting_on(connection):
            yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@asynccontextmanager
async def _accounting_on(connection: AsyncConnection) -> AsyncIterator[None]:
    """Route `log_llm_call` to its own session on `connection` for the duration of a test."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()

    previous = set_accounting_session_factory(factory)
    try:
        yield
    finally:
        set_accounting_session_factory(previous)


@pytest_asyncio.fixture
async def api_learner(db_session: AsyncSession) -> Learner:
    """The learner `api_client` is signed in as.

    Created without a credential: registering would cost an Argon2 hash per test for a
    password nothing checks. What the fixture exercises is the part that runs on every
    request — a session row resolved back to its owner.
    """
    learner = Learner(handle=f"api-{uuid.uuid4().hex[:8]}", display_name="API Test")
    db_session.add(learner)
    await db_session.flush()
    return learner


async def sign_in(client: AsyncClient, session: AsyncSession, learner: Learner) -> str:
    """Give `client` a live session for `learner`, and return the token.

    Used directly by tests that need a *second* authenticated client, which is how the
    cross-learner boundary is tested at all.
    """
    issued = await auth.issue(session, learner, ttl=timedelta(hours=1), commit=False)
    client.cookies.set(get_settings().session_cookie_name, issued.token)
    return issued.token


@asynccontextmanager
async def _app_client(db_session: AsyncSession, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    # The turn lock opens its own connection for an advisory lock (S17). Pointed at this
    # test's engine: a lock taken on the process-wide engine is a lock on a different
    # backend, and its connections are bound to whichever event loop first used them.
    app.dependency_overrides[get_engine] = lambda: engine

    async def _no_judge(_learner_id: uuid.UUID) -> None:
        return None

    # Default no-op: committing a subject enqueues concept-link judging (S24), and the
    # in-memory test broker actually runs a kicked task rather than just queuing it — so
    # without this, an ordinary commit test would reach a real LLM client and its own DB
    # session. A test that cares overrides this dependency itself, same as retag/ingestion.
    app.dependency_overrides[get_concept_link_judge_enqueuer] = lambda: _no_judge
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_client(
    db_session: AsyncSession, engine: AsyncEngine, api_learner: Learner
) -> AsyncIterator[AsyncClient]:
    async with _app_client(db_session, engine) as client:
        await sign_in(client, db_session, api_learner)
        yield client


@pytest_asyncio.fixture
async def anon_client(db_session: AsyncSession, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """The same app with no credential attached."""
    async with _app_client(db_session, engine) as client:
        yield client


@pytest_asyncio.fixture
async def admin_client(db_session: AsyncSession, engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """A client signed in as an administrator (P10).

    Its own learner rather than promoting ``api_learner``: a test that used one client for
    both would stop being able to show that an ordinary learner is refused, which is the
    half of the boundary worth asserting.
    """
    learner = Learner(handle=f"admin-{uuid.uuid4().hex[:8]}", display_name="Admin", is_admin=True)
    db_session.add(learner)
    await db_session.flush()
    async with _app_client(db_session, engine) as client:
        await sign_in(client, db_session, learner)
        yield client
