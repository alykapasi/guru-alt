"""Shared test fixtures.

Each test gets its own NullPool engine (created and disposed inside that test's event
loop, avoiding cross-loop pool reuse), and runs inside an outer transaction that is rolled
back at the end — so tests are isolated and leave the database clean. The session joins
that transaction with savepoints, so application `commit()` calls don't break isolation.

`api_client` drives the FastAPI app in-process (httpx ASGI transport) with `get_session`
overridden to share the test's transactional session.

Cost accounting normally commits on its *own* connection (see `app.services.llm_log`), which
in a test would mean rows referencing learners this transaction has not committed. `accounting`
gives it a second session on the same connection instead: still a separate session whose
commits are its own, but inside the test's transaction, so foreign keys resolve and the rows
roll back with everything else. `test_llm_log.py` covers the real independent-connection
behaviour directly.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.db import get_session
from app.main import app
from app.services.llm_log import set_accounting_session_factory


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
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
async def api_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
