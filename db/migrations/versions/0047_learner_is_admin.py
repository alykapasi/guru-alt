"""give the deployment an administrator, so the operational reads have an owner

Every authenticated learner was the same authenticated learner (S21 left this explicitly
undone). That was survivable while nothing was worth reaching for, and stopped being so the
moment anything surfaced what a deployment spends, how long its models take, and who is using
it — those are reads that need somebody to be allowed to make them.

NOT NULL defaulting to false, and that direction is the whole point: a backfill that guessed
would be guessing *upwards*, and the conservative failure here is a deployment where nobody is
an administrator yet, not one where everybody already is. The first administrator is granted
deliberately (``uv run poe grant-admin``), which is also the only record that anybody decided.

Revision ID: 0047_learner_is_admin
Revises: 0046_subject_ownership
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_learner_is_admin"
down_revision: str | Sequence[str] | None = "0046_subject_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "learners",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("learners", "is_admin")
