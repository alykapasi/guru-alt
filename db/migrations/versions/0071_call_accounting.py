"""Complete call accounting (S48): what each call was for, and how it ended.

A row is now written *before* the call (``pending``, carrying a reserved estimate the spend
guard counts) and settled after (``ok``, ``failed``, ``partial``). Existing rows were only ever
written after success, so they become ``ok``, attributed to no feature (``legacy``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071_call_accounting"
down_revision: str | Sequence[str] | None = "0070_refresh_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_calls", sa.Column("feature", sa.Text(), nullable=False, server_default="legacy")
    )
    op.add_column("llm_calls", sa.Column("request_id", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("status", sa.Text(), nullable=False, server_default="ok"))
    op.add_column("llm_calls", sa.Column("error_kind", sa.Text(), nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("llm_calls", sa.Column("prompt_hash", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("app_version", sa.Text(), nullable=True))
    op.create_index("ix_llm_calls_learner_created", "llm_calls", ["learner_id", "created_at"])
    op.create_index("ix_llm_calls_status", "llm_calls", ["status"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_status", table_name="llm_calls")
    op.drop_index("ix_llm_calls_learner_created", table_name="llm_calls")
    for column in (
        "app_version",
        "prompt_hash",
        "estimated",
        "error_kind",
        "status",
        "request_id",
        "feature",
    ):
        op.drop_column("llm_calls", column)
