"""conversation_goal (refinement gate output)

Revision ID: 0010_conversation_goal
Revises: 0009_chunk_kcs
Create Date: 2026-07-11 00:00:00.000000

Phase 5. Adds the committed goal that the interactive prompt-refinement gate produces once
the learner accepts a proposal. NULL until the gate commits (or was never entered).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_conversation_goal"
down_revision: str | Sequence[str] | None = "0009_chunk_kcs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("conversations", sa.Column("goal", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("conversations", "goal")
