"""memory status: supersession and deletion tombstones

Memory rows were hard-deleted and near-duplicates were skipped (S42). Both lost information:
a learner correcting a preference had the correction discarded in favour of the stale entry,
and a deleted memory could be re-extracted from the same conversation history, because nothing
remained to recognise it by.

Rows are soft-deleted now. The embedding of a deleted row is the tombstone — it is the only
thing that can match the same fact arriving again.

Existing rows become 'current', which is what they were.

Revision ID: 0029_memory_lifecycle
Revises: 0028_refresh_cursors
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_memory_lifecycle"
down_revision: str | Sequence[str] | None = "0028_refresh_cursors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("status", sa.String(), nullable=False, server_default="current"),
    )
    op.add_column("memories", sa.Column("superseded_by_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_memories_superseded_by_id",
        "memories",
        "memories",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_memories_status", "memories", ["status"])
    op.alter_column("memories", "status", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_memories_status", table_name="memories")
    op.drop_constraint("fk_memories_superseded_by_id", "memories", type_="foreignkey")
    op.drop_column("memories", "superseded_by_id")
    op.drop_column("memories", "status")
