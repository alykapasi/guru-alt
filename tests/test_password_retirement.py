"""Retiring the password system without destroying the hashes on the way out (S21).

Dropping `learners.password_hash` in the same change that ships `poe identity-import` would
destroy the one thing the import needs, before any operator could run it. `0055` moves the
digests into a holding table instead, so the order of deploy and import stops mattering: the
column is gone, the hashes are not, and the import empties the table as it succeeds.

Seeding is raw SQL on purpose — see `tests/migration_harness`.
"""

import uuid

import pytest

from tests.migration_harness import database_at, downgrade, upgrade

SCRATCH = "guru_migration_test"

# A real Argon2 digest's shape. Not a real password's — nothing here needs to verify it, and a
# fixture that looked like a credential invites somebody to try.
DIGEST = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHRzYWx0$0123456789abcdefghijklmnopqrstuvwxyz+/A"


async def test_a_learners_digest_moves_to_the_holding_table() -> None:
    """The hash survives the column being dropped, which is the whole point of `0055`."""
    async with database_at("0054_hosted_identity") as connect:
        conn = await connect()
        try:
            with_password = uuid.uuid4()
            without_password = uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle, email, password_hash) VALUES ($1, $2, $3, $4)",
                with_password,
                "has-password",
                "has-password@example.com",
                DIGEST,
            )
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)",
                without_password,
                "never-had-one",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0055_retire_passwords")

        conn = await connect()
        try:
            column = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'learners' AND column_name = 'password_hash'"
            )
            assert column == 0, "the column is dropped, not merely emptied"

            rows = await conn.fetch("SELECT learner_id, digest FROM legacy_password_digests")
            # Exactly one row: a learner who never had a password must not acquire an entry,
            # or the import would try to create a Clerk user for a credential nobody set.
            assert len(rows) == 1
            assert rows[0]["learner_id"] == with_password
            assert rows[0]["digest"] == DIGEST

            # Both learners are still here. Retiring a credential is not deleting an account.
            assert await conn.fetchval("SELECT count(*) FROM learners") == 2
        finally:
            await conn.close()


async def test_downgrading_puts_the_digests_back_on_the_learner() -> None:
    """A downgrade that lost the hashes would make the upgrade unreversible in practice.

    `0055` is the one revision on this branch where going back matters: an operator who rolls
    it back has, by definition, not run the import yet, and the passwords are still the only
    way their learners can sign in.
    """
    async with database_at("0054_hosted_identity") as connect:
        conn = await connect()
        try:
            learner = uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle, email, password_hash) VALUES ($1, $2, $3, $4)",
                learner,
                "round-trip",
                "round-trip@example.com",
                DIGEST,
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0055_retire_passwords")
        await downgrade(SCRATCH, "0054_hosted_identity")

        conn = await connect()
        try:
            restored = await conn.fetchval(
                "SELECT password_hash FROM learners WHERE id = $1", learner
            )
            assert restored == DIGEST

            holding = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = 'legacy_password_digests'"
            )
            assert holding == 0, "the holding table is gone once its contents are back home"
        finally:
            await conn.close()


async def test_the_constraint_comes_back_with_the_column() -> None:
    """`ck_learners_password_requires_email` is dropped with the column and restored with it.

    Left behind on downgrade, a learner could be given a password and no address — an account
    with a credential and no way to recover it.
    """
    async with database_at("0054_hosted_identity") as connect:
        await upgrade(SCRATCH, "0055_retire_passwords")
        await downgrade(SCRATCH, "0054_hosted_identity")

        conn = await connect()
        try:
            with pytest.raises(Exception, match="ck_learners_password_requires_email"):
                await conn.execute(
                    "INSERT INTO learners (id, handle, password_hash) VALUES ($1, $2, $3)",
                    uuid.uuid4(),
                    "no-address",
                    DIGEST,
                )
        finally:
            await conn.close()
