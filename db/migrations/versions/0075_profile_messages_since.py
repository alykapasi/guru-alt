"""Profile message cut-off (O07): ``learners.profile_messages_since``.

Set when a learner turns memory back on; the profile reads only messages written after it, so
nothing typed during a pause is read later. NULL — every existing row — means no cut-off.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075_profile_messages_since"
down_revision: str | Sequence[str] | None = "0074_turn_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("learners", sa.Column("profile_messages_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("learners", "profile_messages_since")
