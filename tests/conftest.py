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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from app.api.deps import get_engine
from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.models.learner import Learner
from app.services import auth
from app.services.llm_log import set_accounting_session_factory


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
