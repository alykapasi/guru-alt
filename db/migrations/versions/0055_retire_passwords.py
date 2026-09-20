"""Retire the password system, keeping the hashes where the import can reach them.

Clerk owns credentials from S21, so Guru stores none. The hashes still matter for exactly one
thing: Clerk accepts an Argon2 digest at import, which is what lets an existing learner keep
the password they already have instead of being told to reset one they never chose to lose.

So this does not drop them. It moves them into `legacy_password_digests`, which
`poe identity-import` empties as each one is accepted. Dropping the column in the same change
that shipped the import would have destroyed the digests before any operator could run it, and
no amount of deploy ordering makes that safe — the two would have had to land in one breath.

Downgrade puts each digest back on its learner and restores the check constraint, because an
operator rolling this back has by definition not imported yet, and those passwords are still
the only way their learners can sign in. `password_reset_tokens` and `sign_in_attempts` come
back empty, which is correct rather than lossy: a reset token is a bearer credential with a
short life, and a throttling counter describes a window that has long since passed. Neither
means anything once restored, and inventing rows to fill them would be inventing events.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_retire_passwords"
down_revision: str | Sequence[str] | None = "0054_hosted_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legacy_password_digests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("digest", sa.String(), nullable=False),
        # Naive, matching TimestampMixin — `poe db-check` catches the mismatch if these drift.
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_legacy_password_digests_learner_id",
        "legacy_password_digests",
        ["learner_id"],
        unique=True,
    )

    # Before the column goes, not after. Only learners who actually set a password get a row:
    # a NULL digest would make the import try to create a Clerk credential nobody chose.
    op.execute(
        """
        INSERT INTO legacy_password_digests (id, learner_id, digest)
        SELECT gen_random_uuid(), id, password_hash
        FROM learners
        WHERE password_hash IS NOT NULL
        """
    )

    op.drop_constraint("ck_learners_password_requires_email", "learners", type_="check")
    op.drop_column("learners", "password_hash")

    op.drop_table("password_reset_tokens")
    op.drop_table("sign_in_attempts")


def downgrade() -> None:
    op.add_column("learners", sa.Column("password_hash", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE learners
        SET password_hash = legacy_password_digests.digest
        FROM legacy_password_digests
        WHERE legacy_password_digests.learner_id = learners.id
        """
    )
    # Added after the digests are back, so a learner who had a password and no address — which
    # the constraint forbids and the upgrade could not have created — cannot block the restore.
    op.create_check_constraint(
        "ck_learners_password_requires_email",
        "learners",
        "password_hash IS NULL OR email IS NOT NULL",
    )

    # Recreated exactly as `0041` built them, columns and indexes alike — a downgrade that
    # leaves a subtly different table is a downgrade that only looks reversible.
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

    op.drop_table("legacy_password_digests")
