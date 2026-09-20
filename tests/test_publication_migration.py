"""Migration `0056`: publication schema, the D5 backfill, and per-owner slugs (S25b).

Three of these pin things an empty-database migration test cannot see. The backfill has a
*direction* that matters — guessing the permissive way is precisely what V03 forbids. The slug
constraints have to allow one thing (two learners, one name) while still forbidding another (one
learner, one name twice), and an index that only does half of that looks identical from outside
until somebody's private subject name shows up in a stranger's slug. And the downgrade is the
only step on this branch that can fail on real rows while passing on empty ones, because this
revision makes legal exactly the data the old constraint rejected.

Seeding is raw SQL on purpose — see `tests/migration_harness`.
"""

import uuid

import pytest

from tests.migration_harness import database_at, downgrade, upgrade

SCRATCH = "guru_migration_test"


async def test_the_backfill_marks_owned_subjects_and_leaves_curated_alone() -> None:
    """D5: nothing ever recorded whether a legacy subject came from uploads.

    So every learner-owned one is assumed to have. The assumption is deliberately the
    inconvenient one — a wrong "no" here publishes somebody's private material, while a wrong
    "yes" only means recreating a subject without uploads.
    """
    async with database_at("0055_retire_passwords") as connect:
        conn = await connect()
        try:
            learner = uuid.uuid4()
            owned, curated = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner, "author"
            )
            await conn.execute(
                "INSERT INTO subjects (id, slug, name, owner_learner_id) VALUES ($1, $2, $3, $4)",
                owned,
                "owned",
                "Owned",
                learner,
            )
            await conn.execute(
                "INSERT INTO subjects (id, slug, name) VALUES ($1, $2, $3)",
                curated,
                "curated",
                "Curated",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0056_publication")

        conn = await connect()
        try:
            assert (
                await conn.fetchval(
                    "SELECT private_source_derived FROM subjects WHERE id = $1", owned
                )
                is True
            )
            assert (
                await conn.fetchval(
                    "SELECT private_source_derived FROM subjects WHERE id = $1", curated
                )
                is False
            ), "a curated subject was never one learner's upload, and flagging it would retire it"
        finally:
            await conn.close()


async def test_two_learners_may_share_a_slug_and_one_learner_may_not() -> None:
    """D8, asserted as behaviour rather than as index names.

    The first insert pair is the leak closing: B's slug stops being an answer about A's
    library. The third is the half that must not be lost along the way — per-owner uniqueness
    is still uniqueness, and without it one learner's two subjects become indistinguishable.
    """
    async with database_at("0055_retire_passwords") as connect:
        await upgrade(SCRATCH, "0056_publication")
        conn = await connect()
        try:
            a, b = uuid.uuid4(), uuid.uuid4()
            for learner_id, handle in ((a, "a"), (b, "b")):
                await conn.execute(
                    "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, handle
                )

            for learner_id in (a, b):
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name, owner_learner_id)"
                    " VALUES ($1, $2, $3, $4)",
                    uuid.uuid4(),
                    "calculus",
                    "Calculus",
                    learner_id,
                )

            with pytest.raises(Exception, match="uq_subjects_owner_slug"):
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name, owner_learner_id)"
                    " VALUES ($1, $2, $3, $4)",
                    uuid.uuid4(),
                    "calculus",
                    "Calculus Again",
                    a,
                )
        finally:
            await conn.close()


async def test_two_curated_subjects_may_not_share_a_slug() -> None:
    """The partial index earns its place here.

    Postgres does not treat NULL owners as equal, so the owner/slug constraint says nothing at
    all about curated rows — on its own it would let the shared library hold two subjects with
    one slug, which is the collision the global index used to prevent.
    """
    async with database_at("0055_retire_passwords") as connect:
        await upgrade(SCRATCH, "0056_publication")
        conn = await connect()
        try:
            await conn.execute(
                "INSERT INTO subjects (id, slug, name) VALUES ($1, $2, $3)",
                uuid.uuid4(),
                "shared",
                "Shared",
            )
            with pytest.raises(Exception, match="uq_subjects_curated_slug"):
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name) VALUES ($1, $2, $3)",
                    uuid.uuid4(),
                    "shared",
                    "Shared Too",
                )
        finally:
            await conn.close()


async def test_downgrade_deduplicates_slugs_instead_of_failing_on_them() -> None:
    """The hazard this revision creates: data it allows, the previous one forbids.

    A downgrade that simply recreated the global unique index would pass every empty-database
    round-trip in CI and then fail on the first real deployment that had two learners studying
    the same thing — during a rollback, which is the worst moment to find out. It renames
    instead, oldest row first so the rename is deterministic.
    """
    async with database_at("0055_retire_passwords") as connect:
        await upgrade(SCRATCH, "0056_publication")
        conn = await connect()
        try:
            a, b = uuid.uuid4(), uuid.uuid4()
            for learner_id, handle in ((a, "a"), (b, "b")):
                await conn.execute(
                    "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, handle
                )
            for learner_id in (a, b):
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name, owner_learner_id)"
                    " VALUES ($1, $2, $3, $4)",
                    uuid.uuid4(),
                    "calculus",
                    "Calculus",
                    learner_id,
                )
        finally:
            await conn.close()

        await downgrade(SCRATCH, "0055_retire_passwords")

        conn = await connect()
        try:
            slugs = sorted(row["slug"] for row in await conn.fetch("SELECT slug FROM subjects"))
            assert slugs == ["calculus", "calculus_2"], slugs
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_name = 'publications'"
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_name = 'subjects' AND column_name = 'private_source_derived'"
                )
                == 0
            )
        finally:
            await conn.close()
