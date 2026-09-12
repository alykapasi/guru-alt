"""Migrations, run against a database that already had rows in it (S58).

CI migrates a fresh database and round-trips every revision, which proves each one *executes*.
The risk a production upgrade actually carries is different: that data already in the table
survives, keeps its meaning, and satisfies whatever constraint the revision adds. A column
added with an ORM-side default and no server default leaves every existing row NULL; a
constraint added without thinking about history refuses to apply at all. Neither shows up
against an empty table.

Seeding is raw SQL on purpose — see `tests/migration_harness`.
"""

import uuid

import pytest

from tests.migration_harness import database_at, upgrade

SCRATCH = "guru_migration_test"


async def test_learners_that_predate_auth_survive_it_with_no_credential() -> None:
    """0039 (S21) adds `email`, `password_hash` and a check constraint to a populated table.

    Every learner in a real database predates it. The migration must not invent an address for
    them — a fabricated `dev@invalid` is a lie that a genuine signup could later collide with —
    and the new constraint must accept the row it leaves behind.
    """
    async with database_at("0038_onboarding_sessions") as connect:
        conn = await connect()
        try:
            existing = uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle, display_name) VALUES ($1, $2, $3)",
                existing,
                "long-standing",
                "Long Standing",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0039_learner_auth")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT handle, display_name, email, password_hash FROM learners WHERE id = $1",
                existing,
            )
            assert row is not None, "the learner did not survive the migration"
            assert row["handle"] == "long-standing"
            assert row["display_name"] == "Long Standing"
            assert row["email"] is None, "an address was invented for a learner who has none"
            assert row["password_hash"] is None

            # The constraint has to tolerate the rows the migration itself leaves behind, and
            # still refuse the combination it exists for.
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", uuid.uuid4(), "another"
            )
            with pytest.raises(Exception, match="ck_learners_password_requires_email"):
                await conn.execute(
                    "INSERT INTO learners (id, handle, password_hash) VALUES ($1, $2, $3)",
                    uuid.uuid4(),
                    "impossible",
                    "$argon2id$whatever",
                )
        finally:
            await conn.close()


async def test_two_learners_without_an_address_do_not_collide_on_the_unique_index() -> None:
    """`email` is unique. Postgres does not count NULLs as equal — but that is a property of
    this database, not of SQL generally, and the whole no-backfill decision rests on it."""
    async with database_at("0038_onboarding_sessions") as connect:
        conn = await connect()
        try:
            for handle in ("first", "second", "third"):
                await conn.execute(
                    "INSERT INTO learners (id, handle) VALUES ($1, $2)", uuid.uuid4(), handle
                )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0039_learner_auth")

        conn = await connect()
        try:
            count = await conn.fetchval("SELECT count(*) FROM learners WHERE email IS NULL")
            assert count == 3
        finally:
            await conn.close()


async def test_conversations_that_predate_the_scaffold_count_get_a_number_not_a_null() -> None:
    """0037 (S15) adds `active_item_scaffolds`, which the code reads and increments.

    An ORM-side `default=0` applies to rows Python creates; it does nothing for rows already in
    the table. If the server default were missing, every conversation in an existing database
    would carry NULL and the first scaffold would increment nothing.
    """
    async with database_at("0036_event_item_exposure") as connect:
        conn = await connect()
        try:
            learner_id = uuid.uuid4()
            conversation_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "talker"
            )
            await conn.execute(
                "INSERT INTO conversations (id, learner_id, phase) VALUES ($1, $2, $3)",
                conversation_id,
                learner_id,
                "chatting",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0037_conversation_scaffolds")

        conn = await connect()
        try:
            scaffolds = await conn.fetchval(
                "SELECT active_item_scaffolds FROM conversations WHERE id = $1", conversation_id
            )
            assert scaffolds == 0, "an existing conversation came through with no scaffold count"
        finally:
            await conn.close()
