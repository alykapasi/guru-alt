"""decision_calls: Jev answers recorded beside today's model decision (S82).

Nothing to backfill: no decision has ever been asked.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062_decision_calls"
down_revision: str | Sequence[str] | None = "0061_concept_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "item_id", sa.Uuid(), sa.ForeignKey("items.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("answer", sa.String(), nullable=True),
        sa.Column("probabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("baseline_intent", sa.String(), nullable=True),
        sa.Column("baseline_score", sa.Float(), nullable=True),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    for column in (
        "request_id",
        "learner_id",
        "conversation_id",
        "item_id",
        "attempt_id",
        "question",
        "created_at",
    ):
        op.create_index(f"ix_decision_calls_{column}", "decision_calls", [column])


def downgrade() -> None:
    op.drop_table("decision_calls")
