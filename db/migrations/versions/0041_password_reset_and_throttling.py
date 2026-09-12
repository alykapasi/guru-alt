"""a way back into an account, and a limit on guessing at one

Sign-in worked and nothing else did. A forgotten password was an operator's problem, by hand,
in the database; there was no way for a learner to change their own credentials; and the
endpoint that spends an Argon2 hash per attempt would do so as many times as anybody asked.

``sign_in_attempts`` records only failures, and only long enough to throttle the next one. In
the database rather than in a process's memory: an in-memory counter is per-process, so a
deployment behind two workers hands an attacker twice the budget and a restart hands them a
fresh one — which throttles the honest user who mistyped and nobody else.

Revision ID: 0041_password_reset_throttle
Revises: 0040_message_check_result
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_password_reset_throttle"
down_revision: str | Sequence[str] | None = "0040_message_check_result"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        # Naive, matching TimestampMixin — `poe db-check` catches the mismatch if these drift.
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_password_reset_tokens_learner_id", "password_reset_tokens", ["learner_id"])
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )

    op.create_table(
        "sign_in_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_sign_in_attempts_email", "sign_in_attempts", ["email"])
    op.create_index("ix_sign_in_attempts_client", "sign_in_attempts", ["client"])
    op.create_index("ix_sign_in_attempts_created_at", "sign_in_attempts", ["created_at"])


def downgrade() -> None:
    op.drop_table("sign_in_attempts")
    op.drop_table("password_reset_tokens")
