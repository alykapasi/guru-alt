"""Item settings (S14): the named setting a question is set in, for transfer.

Nullable, and NULL is read as abstract (``app.learning.transfer``), so existing items need no
backfill: nearly all of them were written with no setting at all.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073_item_settings"
down_revision: str | Sequence[str] | None = "0072_grading_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("setting", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "setting")
