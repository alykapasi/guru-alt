"""Account deletion with a recovery window, and erasures that are retried (S61, V12).

``deletion_requested_at``/``deletion_due_at`` make deletion a state rather than an event: access
ends at once, the data stays for the recovery window, then a worker erases. ``pending_erasures``
holds what could not be erased when asked — object-store keys and identity-provider users —
naming no learner, so it survives the erase it belongs to and is retried until it succeeds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_account_deletion_erasures"
down_revision: str | Sequence[str] | None = "0066_archive_and_memory_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learners", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "learners", sa.Column("deletion_due_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_learners_deletion_due_at", "learners", ["deletion_due_at"])
    op.create_table(
        "pending_erasures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("kind", "target", name="uq_pending_erasures_kind_target"),
    )
    op.create_index("ix_pending_erasures_next_attempt_at", "pending_erasures", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_erasures_next_attempt_at", table_name="pending_erasures")
    op.drop_table("pending_erasures")
    op.drop_index("ix_learners_deletion_due_at", table_name="learners")
    op.drop_column("learners", "deletion_due_at")
    op.drop_column("learners", "deletion_requested_at")
