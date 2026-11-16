"""messages.turn_id: the turn whose attempt kept a cut-off reply

Revision ID: 0079_message_turn_id
Revises: 0078_s59_eval_examples
Create Date: 2026-11-16 13:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0079_message_turn_id"
down_revision: str | Sequence[str] | None = "0078_s59_eval_examples"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("turn_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("messages", "turn_id")
