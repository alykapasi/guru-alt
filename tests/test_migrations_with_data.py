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

from app.core.config import get_settings
from tests.migration_harness import database_at, downgrade, upgrade

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


async def test_blocks_that_predate_the_grounding_count_come_through_as_unknown_not_zero() -> None:
    """0042 (S28) adds `grounding_count`, and deliberately does *not* backfill it.

    The inverse of the case above: here a server default would be the bug. Zero is a claim —
    "this block was written with no source material" — and for a row that predates the column
    it is a claim nobody measured. The grounding set was never stored, and `citations` counts
    what was cited rather than what was offered, so there is nothing to reconstruct from.
    Backfilling would put a measurement on rows that were never measured; NULL says "not
    recorded", which is the true thing to say about them.
    """
    async with database_at("0041_password_reset_throttle") as connect:
        conn = await connect()
        try:
            learner_id, kc_id, block_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "reader"
            )
            await conn.execute(
                "INSERT INTO content_blocks "
                "(id, learner_id, kc_ids, block_type, body, citations, cache_key, model) "
                "VALUES ($1, $2, $3::uuid[], $4, $5, $6::jsonb, $7, $8)",
                block_id,
                learner_id,
                [kc_id],
                "lesson",
                "A lesson written before anyone counted the grounding.",
                "[]",
                "key-that-predates-0042",
                "some-model-1",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0042_content_grounding_count")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT grounding_count, body FROM content_blocks WHERE id = $1", block_id
            )
            assert row is not None, "the block did not survive the upgrade"
            assert row["grounding_count"] is None, "backfilled a measurement nobody took"
        finally:
            await conn.close()


async def test_calls_recorded_before_timing_come_through_unmeasured_not_instant() -> None:
    """0043 (S48) adds `latency_ms`, nullable and unbackfilled for the same reason as 0042.

    `cost_usd` on this same table already draws the distinction the column needs: NULL means
    unpriced, 0.0 means it ran locally and cost nothing, and collapsing them reported unpriced
    spend as free. Zero latency would claim an instantaneous call — a far more flattering lie
    than the missing measurement it would be standing in for.
    """
    async with database_at("0042_content_grounding_count") as connect:
        conn = await connect()
        try:
            call_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO llm_calls (id, role, provider, model, input_tokens, output_tokens) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                call_id,
                "smart",
                "anthropic",
                "claude-sonnet-4-6",
                100,
                20,
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0043_llm_call_latency")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT latency_ms, input_tokens FROM llm_calls WHERE id = $1", call_id
            )
            assert row is not None, "the call did not survive the upgrade"
            assert row["input_tokens"] == 100, "the accounting it did hold was not disturbed"
            assert row["latency_ms"] is None, "claimed a timing that was never taken"
        finally:
            await conn.close()


async def test_a_provider_link_survives_the_round_trip_down_and_back_up() -> None:
    """0054 (S21) adds ``auth_subject``. A downgrade has to be able to drop it from a table that
    already has a row carrying one, and the subsequent upgrade has to restore the column and
    its unique index without choking on the row that is already there.
    """
    async with database_at("0054_hosted_identity") as connect:
        conn = await connect()
        try:
            learner_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle, auth_subject) VALUES ($1, $2, $3)",
                learner_id,
                "hosted",
                "user_abc123",
            )
        finally:
            await conn.close()

        await downgrade(SCRATCH, "0053_note_exact_authorship")
        conn = await connect()
        try:
            row = await conn.fetchrow("SELECT handle FROM learners WHERE id = $1", learner_id)
            assert row is not None, "the learner did not survive the downgrade"
            assert row["handle"] == "hosted"
            columns = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'learners'"
            )
            assert "auth_subject" not in {c["column_name"] for c in columns}
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0054_hosted_identity")
        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT handle, auth_subject FROM learners WHERE id = $1", learner_id
            )
            assert row is not None, "the learner did not survive the re-upgrade"
            assert row["handle"] == "hosted"
            assert row["auth_subject"] is None, "the column came back with no way to fill it"

            await conn.execute(
                "INSERT INTO learners (id, handle, auth_subject) VALUES ($1, $2, $3)",
                uuid.uuid4(),
                "second",
                "user_def456",
            )
            with pytest.raises(Exception, match="ix_learners_auth_subject"):
                await conn.execute(
                    "INSERT INTO learners (id, handle, auth_subject) VALUES ($1, $2, $3)",
                    uuid.uuid4(),
                    "third",
                    "user_def456",
                )
        finally:
            await conn.close()


async def test_chunks_that_predate_versions_come_through_as_version_one_and_current() -> None:
    """0064 (S29/S50): every existing chunk was written by pipeline 1, and none is superseded."""
    async with database_at("0063_source_scope_settings") as connect:
        conn = await connect()
        try:
            learner_id, source_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            dim = get_settings().embed_dim
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "reader"
            )
            await conn.execute(
                "INSERT INTO sources (id, learner_id, kind, origin, status, meta, attempts) "
                "VALUES ($1, $2, 'file', 'notes.txt', 'done', '{}'::jsonb, 0)",
                source_id,
                learner_id,
            )
            await conn.execute(
                "INSERT INTO chunks (id, source_id, ordinal, text, embedding, embedding_space, "
                "provenance) VALUES ($1, $2, 0, 'old text', $3::vector, 'fake:fake-1:x', "
                "'{}'::jsonb)",
                chunk_id,
                source_id,
                "[" + ",".join(["0.1"] * dim) + "]",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0064_chunk_versions")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT pipeline_version, superseded_at, text FROM chunks WHERE id = $1",
                chunk_id,
            )
            assert row is not None, "the chunk did not survive the upgrade"
            assert row["pipeline_version"] == 1
            assert row["superseded_at"] is None
            assert row["text"] == "old text"
        finally:
            await conn.close()
