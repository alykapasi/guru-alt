"""The suite's own database: derive its URL, create it, and bring it to head.

Tests assert on *global* rows — total LLM calls, event counts — and create the fixed dev
learner by handle, so they need a database nobody else is writing to. Sharing one with a
running dev server is enough to fail them for reasons unrelated to the code under test.
Rather than a second DSN to keep in sync, the test database is *derived* from the
configured one by suffixing its name, so it inherits whatever host and credentials the
developer (``.env``) or CI (``GURU_DATABASE_URL``) already set.

``python -m tests.testdb`` (``poe test-db-init``, a dependency of ``poe test``) creates it
if missing and migrates it. Module scope is deliberately free of ``app`` imports: the root
``conftest.py`` imports this *before* application settings are read and cached.
"""

import asyncio
import os
import sys

import asyncpg
from sqlalchemy.engine import make_url

TEST_SUFFIX = "_test"


def test_database_url(url: str) -> str:
    """The test twin of ``url``: same host and credentials, ``_test`` database.

    Idempotent — a URL already naming a ``_test`` database is returned unchanged, so
    pointing ``GURU_DATABASE_URL`` straight at one works too.
    """
    parsed = make_url(url)
    name = parsed.database or "guru"
    if name.endswith(TEST_SUFFIX):
        return url
    # str(URL) masks the password; rendering explicitly keeps the DSN usable.
    return parsed.set(database=f"{name}{TEST_SUFFIX}").render_as_string(hide_password=False)


async def _create_if_missing(url: str) -> bool:
    """Create the database if it doesn't exist yet. Returns True if it was created."""
    parsed = make_url(url)
    name = parsed.database
    if not name or '"' in name:
        raise ValueError(f"refusing to create a database named {name!r}")
    # CREATE DATABASE can't run inside a transaction or against the target itself, so this
    # goes through the always-present `postgres` maintenance database on the same server.
    conn = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        if await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            return False
        await conn.execute(f'CREATE DATABASE "{name}"')
        return True
    finally:
        await conn.close()


def main() -> None:
    from app.core.config import Settings

    url = test_database_url(Settings().database_url)
    created = asyncio.run(_create_if_missing(url))
    # Alembic's env.py reads the URL from settings, so hand it over the same way the root
    # conftest does rather than setting it on the Config (which env.py would overwrite).
    os.environ["GURU_DATABASE_URL"] = url

    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    name = make_url(url).database
    print(f"test database {name!r} {'created and ' if created else ''}at head", file=sys.stderr)


if __name__ == "__main__":
    main()
