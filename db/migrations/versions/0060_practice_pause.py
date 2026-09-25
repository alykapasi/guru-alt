"""Add conversations.practice_scaffolds (S52).

Guided practice can now be paused for a side discussion and explicitly resumed or skipped.
Tutor replies given while a question is paused count as help toward the attempt that
eventually gets graded, the same discount ``active_item_scaffolds`` already gives a
scaffolded conversational check (S15) — and for the same reason a column, not an inference:
the reply that helps and the round it discounts are written in different transactions, so
``created_at`` is transaction time and ordering the two by clock is exactly the kind of guess
this column exists to avoid.

No backfill. The server default ``0`` *is* today's behaviour for every existing conversation —
nothing has ever paused, so there is nothing to compute.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060_practice_pause"
down_revision: str | Sequence[str] | None = "0059_detour_guidance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("practice_scaffolds", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "practice_scaffolds")
