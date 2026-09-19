"""Durable alpha sudo action audit and revoke borrowed sessions on admin deletion."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_admin_actions"
down_revision: str | Sequence[str] | None = "0049_impersonation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Cascading deletion also updates the historical parent; defer its session FK until
    # all SET NULL/CASCADE triggers have completed.
    op.execute(
        "ALTER TABLE impersonations ALTER CONSTRAINT impersonations_session_id_fkey DEFERRABLE INITIALLY DEFERRED"
    )
    op.drop_constraint(
        "fk_learner_sessions_impersonated_by_id_learners", "learner_sessions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_learner_sessions_impersonated_by_id_learners",
        "learner_sessions",
        "learners",
        ["impersonated_by_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "admin_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "impersonation_id", sa.Uuid(), sa.ForeignKey("impersonations.id"), nullable=False
        ),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("route", sa.String(), nullable=False),
        sa.Column("resource_ids", sa.JSON(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_admin_actions_impersonation_id", "admin_actions", ["impersonation_id"])


def downgrade() -> None:
    op.execute(
        "ALTER TABLE impersonations ALTER CONSTRAINT impersonations_session_id_fkey NOT DEFERRABLE"
    )
    op.drop_table("admin_actions")
    op.drop_constraint(
        "fk_learner_sessions_impersonated_by_id_learners", "learner_sessions", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_learner_sessions_impersonated_by_id_learners",
        "learner_sessions",
        "learners",
        ["impersonated_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
