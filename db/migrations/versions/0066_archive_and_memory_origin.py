"""Archive for sources and conversations; where a memory came from (S61, V11).

``archived_at`` puts a source or conversation out of the way and out of use without deleting
anything. ``memories.origin_conversation_id`` copies ``conversation_id`` with no foreign key:
the FK nulls when the conversation is deleted, and the copy is what lets a learner still
forget what was learned there afterwards.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_archive_and_memory_origin"
down_revision: str | Sequence[str] | None = "0065_source_duplicate_of"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("memories", sa.Column("origin_conversation_id", sa.Uuid(), nullable=True))
    op.create_index("ix_memories_origin_conversation_id", "memories", ["origin_conversation_id"])
    op.execute("UPDATE memories SET origin_conversation_id = conversation_id")


def downgrade() -> None:
    op.drop_index("ix_memories_origin_conversation_id", table_name="memories")
    op.drop_column("memories", "origin_conversation_id")
    op.drop_column("conversations", "archived_at")
    op.drop_column("sources", "archived_at")
