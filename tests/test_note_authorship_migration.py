"""Exercise0053 against legacy rows in connection-local temporary tables."""

import importlib.util
import json
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_exact_authorship_migration_recovers_only_reliable_history(
    db_session: AsyncSession,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "note_authorship_migration", Path("db/migrations/versions/0053_note_exact_authorship.py")
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db_session.connection()
    # Temporary relations shadow the real product-test tables only on this connection.
    await connection.execute(
        text(
            "CREATE TEMP TABLE notes (id uuid PRIMARY KEY, revision_ordinal integer NOT NULL) ON COMMIT DROP"
        )
    )
    await connection.execute(
        text(
            "CREATE TEMP TABLE note_revisions (id uuid PRIMARY KEY, note_id uuid NOT NULL, ordinal integer NOT NULL, cause text NOT NULL, substrate jsonb NOT NULL, learner_edit_md text) ON COMMIT DROP"
        )
    )
    await connection.execute(
        text("CREATE TEMP TABLE note_renders (note_id uuid NOT NULL) ON COMMIT DROP")
    )
    reliable, restored, missing = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    typed = "# 私の 🧠\r\n\r\n- e\u0301  \r\n- first\r\n"
    atom = {
        "id": "a1",
        "md": "model interpretation",
        "kind": "concept",
        "kc_ids": [],
        "provenance": {},
    }
    for note_id, ordinal in ((reliable, 3), (restored, 4), (missing, 2)):
        await connection.execute(
            text("INSERT INTO notes VALUES (:id,:ordinal)"), {"id": note_id, "ordinal": ordinal}
        )
        await connection.execute(text("INSERT INTO note_renders VALUES (:id)"), {"id": note_id})
    histories = {
        reliable: [(1, "distill", None), (2, "learner_edit", typed), (3, "distill", None)],
        restored: [
            (1, "distill", None),
            (2, "learner_edit", typed),
            (3, "restore", None),
            (4, "distill", None),
        ],
        missing: [(1, "learner_edit", None), (2, "distill", None)],
    }
    for note_id, history in histories.items():
        for ordinal, cause, raw in history:
            await connection.execute(
                text(
                    "INSERT INTO note_revisions VALUES (:id,:note,:ordinal,:cause,CAST(:atoms AS jsonb),:raw)"
                ),
                {
                    "id": uuid.uuid4(),
                    "note": note_id,
                    "ordinal": ordinal,
                    "cause": cause,
                    "atoms": json.dumps([atom]),
                    "raw": raw,
                },
            )

    def upgrade(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.upgrade()

    await connection.run_sync(upgrade)
    recovered = {
        row.id: row for row in (await connection.execute(text("SELECT * FROM notes"))).all()
    }
    assert recovered[reliable].learner_authored_md.encode() == typed.encode()
    assert recovered[reliable].authored_baseline == {"a1": "model interpretation"}
    assert recovered[restored].learner_authored_md is None
    assert recovered[missing].learner_authored_md is None
    history = (
        await connection.execute(
            text("SELECT * FROM note_revisions WHERE note_id=:note ORDER BY ordinal"),
            {"note": restored},
        )
    ).all()
    assert history[1].learner_edit_md == typed
    assert history[1].learner_authored_md == typed
    assert history[2].learner_authored_md is None
    assert history[3].learner_authored_md is None
    inherited = (
        await connection.execute(
            text(
                "SELECT learner_authored_md FROM note_revisions WHERE note_id=:note AND ordinal=3"
            ),
            {"note": reliable},
        )
    ).scalar_one()
    assert inherited == typed
    remaining = (await connection.execute(text("SELECT note_id FROM note_renders"))).scalars().all()
    assert reliable not in remaining and restored in remaining and missing in remaining
