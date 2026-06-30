"""learner_kc_state: make last_seen_at / due_at timezone-aware

Revision ID: 0004_kc_state_tz
Revises: 0003_chat_core
Create Date: 2026-06-30 13:10:00.000000

The tracer does elapsed-time arithmetic on these columns (uncertainty decay now, FSRS
scheduling in slice 5), so they must store real UTC instants, not naive local timestamps.
``USING ... AT TIME ZONE 'UTC'`` reinterprets any existing naive values as UTC.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_kc_state_tz"
down_revision: str | Sequence[str] | None = "0003_chat_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "learner_kc_state",
        "last_seen_at",
        type_=sa.DateTime(timezone=True),
        postgresql_using="last_seen_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "learner_kc_state",
        "due_at",
        type_=sa.DateTime(timezone=True),
        postgresql_using="due_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "learner_kc_state",
        "due_at",
        type_=sa.DateTime(),
        postgresql_using="due_at AT TIME ZONE 'UTC'",
    )
    op.alter_column(
        "learner_kc_state",
        "last_seen_at",
        type_=sa.DateTime(),
        postgresql_using="last_seen_at AT TIME ZONE 'UTC'",
    )
