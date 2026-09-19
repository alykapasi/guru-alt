"""Separate exact learner Markdown from generated atoms.

Legacy restore did not record its target, so it is an authorship barrier: do not
infer that an identical atom snapshot proves a particular learner edit was restored.
Raw learner_edit_md remains available in history even across an ambiguous restore.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0053_note_exact_authorship"
down_revision: str | Sequence[str] | None = "0052_assessment_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("notes", "note_revisions"):
        op.add_column(table, sa.Column("learner_authored_md", sa.Text(), nullable=True))
        op.add_column(
            table, sa.Column("authored_baseline", JSONB(), nullable=False, server_default="{}")
        )
    connection = op.get_bind()
    revisions = sa.table(
        "note_revisions",
        sa.column("id"),
        sa.column("note_id"),
        sa.column("ordinal"),
        sa.column("cause"),
        sa.column("substrate", JSONB()),
        sa.column("learner_edit_md"),
        sa.column("learner_authored_md"),
        sa.column("authored_baseline", JSONB()),
    )
    notes = sa.table(
        "notes",
        sa.column("id"),
        sa.column("revision_ordinal"),
        sa.column("learner_authored_md"),
        sa.column("authored_baseline", JSONB()),
    )
    # Stream snapshots in history order rather than retaining all learner text in memory.
    current_note = None
    authored = None
    baseline = {}
    for row in connection.execute(
        sa.select(revisions).order_by(revisions.c.note_id, revisions.c.ordinal)
    ).mappings():
        if row["note_id"] != current_note:
            current_note, authored, baseline = row["note_id"], None, {}
        if row["cause"] == "learner_edit" and row["learner_edit_md"] is not None:
            authored = row["learner_edit_md"]
            baseline = {a["id"]: a["md"] for a in row["substrate"] if "id" in a and "md" in a}
        elif row["cause"] in ("restore", "learner_edit"):
            authored, baseline = None, {}
        connection.execute(
            revisions.update()
            .where(revisions.c.id == row["id"])
            .values(learner_authored_md=authored, authored_baseline=baseline)
        )
        connection.execute(
            notes.update()
            .where(notes.c.id == current_note, notes.c.revision_ordinal == row["ordinal"])
            .values(learner_authored_md=authored, authored_baseline=baseline)
        )
    # Legacy cached projections do not include recovered exact Markdown.
    connection.execute(
        sa.text(
            "DELETE FROM note_renders WHERE note_id IN (SELECT id FROM notes WHERE learner_authored_md IS NOT NULL)"
        )
    )


def downgrade() -> None:
    for table in ("note_revisions", "notes"):
        op.drop_column(table, "authored_baseline")
        op.drop_column(table, "learner_authored_md")
