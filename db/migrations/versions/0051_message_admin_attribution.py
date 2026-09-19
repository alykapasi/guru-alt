"""Preserve administrator identity in transcript snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_message_admin_attribution"
down_revision: str | Sequence[str] | None = "0050_admin_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("admin_actor_id", sa.Uuid(), nullable=True))
    op.add_column("messages", sa.Column("admin_action_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "admin_action_id")
    op.drop_column("messages", "admin_actor_id")
