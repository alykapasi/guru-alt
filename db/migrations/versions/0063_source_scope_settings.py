"""Per-subject source switches and per-message grounding counts (S26, S28).

Nothing to backfill. Both switches start off, which is the V05 default: untagged material is
not added to a subject until its owner says so. A message written before this has no count,
and NULL says exactly that — it is not zero, because nobody measured it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_source_scope_settings"
down_revision: str | Sequence[str] | None = "0062_decision_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column(
            "include_untagged_sources", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "subjects",
        sa.Column("sources_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("messages", sa.Column("grounding_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "grounding_count")
    op.drop_column("subjects", "sources_only")
    op.drop_column("subjects", "include_untagged_sources")
