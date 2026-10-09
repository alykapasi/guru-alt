"""The checkpointer's schema is the deploy step's job, not every process's (S17).

Every process used to run LangGraph's ``setup()`` as it started: DDL from the app's role, and
two processes starting together raced on ``checkpoint_migrations`` — the loser fell back to
volatile state. Now ``poe db-upgrade`` migrates it once, under a lock, and a process only
checks the version it finds.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.agent import checkpointing
from app.core.config import get_settings


@pytest_asyncio.fixture
async def empty_schema() -> AsyncIterator[str]:
    """A conninfo whose search path is a brand-new, empty schema — a database never migrated."""
    name = f"s17_{uuid.uuid4().hex[:8]}"
    base = checkpointing.psycopg_dsn(get_settings().database_url)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(base, options=f"-c search_path={name}")
    finally:
        async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


async def _version_at(conninfo: str) -> int | None:
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        return await checkpointing.schema_version(conn)


async def test_an_unmigrated_database_has_no_version(empty_schema: str) -> None:
    assert await _version_at(empty_schema) is None


async def test_migrate_brings_the_schema_to_the_libraries_version(empty_schema: str) -> None:
    assert await checkpointing.migrate(empty_schema) == checkpointing.expected_schema_version()
    assert await _version_at(empty_schema) == len(AsyncPostgresSaver.MIGRATIONS) - 1


async def test_two_deploys_migrating_at_once_both_succeed(empty_schema: str) -> None:
    """Overlapping deploys take turns on the advisory lock instead of racing the DDL."""
    first, second = await asyncio.gather(
        checkpointing.migrate(empty_schema), checkpointing.migrate(empty_schema)
    )
    assert first == second == checkpointing.expected_schema_version()


async def test_start_runs_no_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(_self: object) -> None:
        raise AssertionError("start() must not run setup()")

    monkeypatch.setattr(AsyncPostgresSaver, "setup", refuse)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is True
    finally:
        await checkpointing.stop()


async def test_start_falls_back_when_the_schema_is_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A LangGraph upgrade deployed without ``poe db-upgrade``: serve chat, volatile, loudly."""
    real = checkpointing.expected_schema_version()
    monkeypatch.setattr(checkpointing, "expected_schema_version", lambda: real + 1)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is False
    finally:
        await checkpointing.stop()


async def test_start_falls_back_when_the_schema_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(_conn: object) -> None:
        return None

    monkeypatch.setattr(checkpointing, "schema_version", missing)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is False
    finally:
        await checkpointing.stop()


async def test_start_stays_durable_when_the_schema_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer release already migrated it: the tables are a superset, so keep them."""
    real = checkpointing.expected_schema_version()
    monkeypatch.setattr(checkpointing, "expected_schema_version", lambda: real - 1)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is True
    finally:
        await checkpointing.stop()
