"""learner_kc_state: add fsrs_card + index due_at for review scheduling

Revision ID: 0006_kc_state_fsrs
Revises: 0005_assessment
Create Date: 2026-06-30 15:00:00.000000

Slice 5 adds FSRS retention scheduling. The serialized FSRS memory card is stored opaquely
in ``fsrs_card`` (JSONB); ``due_at`` gets an index because ``due_reviews`` scans by it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_kc_state_fsrs"
down_revision: str | Sequence[str] | None = "0005_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "learner_kc_state",
        sa.Column("fsrs_card", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        op.f("ix_learner_kc_state_due_at"), "learner_kc_state", ["due_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_learner_kc_state_due_at"), table_name="learner_kc_state")
    op.drop_column("learner_kc_state", "fsrs_card")
