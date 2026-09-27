"""Refresh scheduling (S43): memory consent, claim stamps, and estimator input fingerprints.

``learners.remember_conversations`` is the learner's switch for memory; existing learners keep
it on, which is today's behaviour. ``memory_attempted_at`` / ``refresh_attempted_at`` are the
scheduler's claims: set when a pass queues the work, cleared when it succeeds, so a failure is
retried after a delay rather than every pass. ``input_fingerprint`` records what a model-backed
estimator last judged, so an unchanged sample costs no model call.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_refresh_scheduling"
down_revision: str | Sequence[str] | None = "0069_memory_forgotten_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learners",
        sa.Column("remember_conversations", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column("conversations", sa.Column("memory_attempted_at", sa.DateTime(), nullable=True))
    op.add_column(
        "learner_profiles", sa.Column("refresh_attempted_at", sa.DateTime(), nullable=True)
    )
    op.add_column("profile_dimensions", sa.Column("input_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profile_dimensions", "input_fingerprint")
    op.drop_column("learner_profiles", "refresh_attempted_at")
    op.drop_column("conversations", "memory_attempted_at")
    op.drop_column("learners", "remember_conversations")
