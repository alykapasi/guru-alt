"""Stopped turns (S47): how an assistant reply was cut short, when it was.

``messages.interrupted`` is ``stopped`` (the learner pressed Stop) or ``timed_out`` (the turn
deadline expired); NULL is a reply that finished, which is every existing row. The turn status
``stopped`` is a new string in an existing text column and needs no DDL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_turn_stop"
down_revision: str | Sequence[str] | None = "0073_item_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("interrupted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "interrupted")
