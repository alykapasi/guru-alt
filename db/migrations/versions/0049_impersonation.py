"""record every administrator's visit to a learner's account, and mark the session it issued

P10's support half. An administrator who needs to see why a learner's upload never became a
lesson has, until now, had two options: ask for their password, or read the database by hand.
The first is never acceptable and the second leaves no record that anybody looked.

Two changes, and they answer different questions. ``learner_sessions.impersonated_by_id`` is
what the *request path* reads — every authenticated request already loads this row, so asking
"is this really its learner?" costs nothing here and would cost a join anywhere else. The
``impersonations`` table is the *record*, and it is a separate table precisely because sessions
are purged once they can no longer authenticate anybody: an audit that the session sweep
deletes is an audit with a retention policy nobody chose.

Both foreign keys in ``impersonations`` are SET NULL and both handles are stored as text
beside them. A record a deleted learner erases is not an audit of access to that learner's
data, and one an administrator erases by closing their own account is not an audit of
anything. The handles keep the row meaningful when an id has gone; ``app.services.retention``
states which of them survives account deletion and which the service clears.

Revision ID: 0049_impersonation
Revises: 0048_llm_call_first_token
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_impersonation"
down_revision: str | Sequence[str] | None = "0048_llm_call_first_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "learner_sessions",
        sa.Column("impersonated_by_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_learner_sessions_impersonated_by_id", "learner_sessions", ["impersonated_by_id"]
    )
    op.create_foreign_key(
        "fk_learner_sessions_impersonated_by_id_learners",
        "learner_sessions",
        "learners",
        ["impersonated_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "impersonations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("admin_learner_id", sa.Uuid(), nullable=True),
        sa.Column("admin_handle", sa.String(), nullable=False),
        sa.Column("learner_id", sa.Uuid(), nullable=True),
        sa.Column("learner_handle", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        # Naive, matching ``TimestampMixin`` — it declares no timezone, and `poe db-check`
        # reports the difference as schema drift rather than letting it sit.
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["admin_learner_id"], ["learners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["learner_sessions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_impersonations_admin_learner_id", "impersonations", ["admin_learner_id"])
    op.create_index("ix_impersonations_learner_id", "impersonations", ["learner_id"])


def downgrade() -> None:
    op.drop_table("impersonations")
    op.drop_constraint(
        "fk_learner_sessions_impersonated_by_id_learners", "learner_sessions", type_="foreignkey"
    )
    op.drop_index("ix_learner_sessions_impersonated_by_id", table_name="learner_sessions")
    op.drop_column("learner_sessions", "impersonated_by_id")
