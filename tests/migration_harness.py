"""Run a migration against a database that already has data in it (S58).

Every migration in this repo is tested against an *empty* database — CI migrates a fresh one,
round-trips every revision, and the suite creates its own. That proves a revision executes. It
does not prove the thing a production upgrade actually risks: that rows already in the table
survive it, keep their meaning, and satisfy whatever constraint the revision adds.

The harness gives a test a scratch database at a chosen revision, a plain connection to seed
it, and then the upgrade under test. Two rules make the results mean anything:

*Seed with SQL, never with the ORM.* The models describe `head`. Inserting through them at an
older revision either fails or, worse, quietly writes columns the old schema is not supposed to
have — and the migration then "passes" against data no real deployment could have contained.

*Use a scratch database.* The suite's own database is shared by every other test, and
downgrading it underneath them is not a migration test, it is a flaky suite.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import asyncpg
from sqlalchemy.engine import make_url


def _admin_dsn(url: str) -> dict:
    parsed = make_url(url)
    return {
        "host": parsed.host,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "database": "postgres",
    }


def _asyncpg_dsn(url: str) -> dict:
    parsed = make_url(url)
    return {
        "host": parsed.host,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "database": parsed.database,
    }


async def _recreate(url: str) -> None:
    name = make_url(url).database
    if not name or '"' in name:
        raise ValueError(f"refusing to create a database named {name!r}")
    conn = await asyncpg.connect(**_admin_dsn(url))
    try:
        # FORCE, because a connection left open by a previous failing run would otherwise make
        # every subsequent run fail for a reason that has nothing to do with the migration.
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


async def _drop(url: str) -> None:
    name = make_url(url).database
    conn = await asyncpg.connect(**_admin_dsn(url))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def _alembic(url: str, revision: str) -> None:
    """Migrate ``url`` to ``revision``. Blocking — call it through :func:`_migrate`.

    Alembic's ``env.py`` reads the URL from application settings, so it is handed over through
    the environment the same way ``tests/testdb.py`` does — setting it on the Config object
    would simply be overwritten.
    """
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("GURU_DATABASE_URL")
    os.environ["GURU_DATABASE_URL"] = url
    try:
        from app.core.config import get_settings

        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        if previous is None:
            os.environ.pop("GURU_DATABASE_URL", None)
        else:
            os.environ["GURU_DATABASE_URL"] = previous
        from app.core.config import get_settings

        get_settings.cache_clear()


async def _migrate(url: str, revision: str) -> None:
    """Run a migration from async code.

    In a worker thread, because the project's ``env.py`` drives an async engine with
    ``asyncio.run`` — which refuses to start inside a loop that is already running, and a test
    is always inside one.
    """
    await asyncio.to_thread(_alembic, url, revision)


@asynccontextmanager
async def database_at(
    revision: str, *, name: str = "guru_migration_test"
) -> AsyncIterator[Callable[[], Awaitable[asyncpg.Connection]]]:
    """A scratch database migrated to ``revision``, yielding a connection factory.

    The factory opens plain asyncpg connections: no ORM, no session, nothing that knows what
    the schema looks like at ``head``.
    """
    from app.core.config import get_settings

    base = make_url(get_settings().database_url)
    url = base.set(database=name).render_as_string(hide_password=False)

    await _recreate(url)
    try:
        await _migrate(url, revision)

        async def connect() -> asyncpg.Connection:
            return await asyncpg.connect(**_asyncpg_dsn(url))

        yield connect
    finally:
        await _drop(url)


async def upgrade(name: str, revision: str) -> None:
    """Bring the scratch database named ``name`` to ``revision`` — the upgrade under test."""
    from app.core.config import get_settings

    base = make_url(get_settings().database_url)
    await _migrate(base.set(database=name).render_as_string(hide_password=False), revision)
