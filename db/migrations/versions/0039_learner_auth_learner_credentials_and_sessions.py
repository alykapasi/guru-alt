"""learner credentials, and sessions that can be withdrawn

Replaces the development identity stub (S21). ``get_current_learner`` used to resolve — and
create — a single dev learner for any caller, so ``learner_id`` was threaded everywhere behind
a boundary that was not there. This adds the two things that make it real: a credential on the
learner, and a server-side session row that a request is checked against and that can be
revoked before it expires.

Both credential columns are nullable, so nothing is backfilled and no existing row is given a
fabricated address. Every learner that predates this migration simply has no way to sign in,
which is the truth about them; the development sign-in seam (``POST /auth/dev-login``, off in
production) is how the dev learner still gets a session.

Revision ID: 0039_learner_auth
Revises: 0038_onboarding_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_learner_auth"
down_revision: str | Sequence[str] | None = "0038_onboarding_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("learners", sa.Column("email", sa.String(), nullable=True))
    op.add_column("learners", sa.Column("password_hash", sa.String(), nullable=True))
    # Unique, and Postgres does not treat NULLs as equal, so any number of credential-less
    # learners coexist while one address can only ever belong to one account.
    op.create_index("ix_learners_email", "learners", ["email"], unique=True)
    # A password with no address is the one combination that cannot be signed in with and
    # cannot be recovered from. Refused here rather than documented in the model.
    op.create_check_constraint(
        "ck_learners_password_requires_email",
        "learners",
        "password_hash IS NULL OR email IS NOT NULL",
    )

    op.create_table(
        "learner_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # CASCADE: a deleted account's sessions must stop authenticating anybody at the moment
        # the account goes, not at the moment they would have expired.
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The SHA-256 fingerprint of the token, never the token: a dump, a backup (S60) or a
        # log line must not be replayable as a login.
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    # The lookup on every authenticated request, and the uniqueness that makes a fingerprint
    # collision an integrity error rather than one learner resolving to another's session.
    op.create_index(
        "ix_learner_sessions_token_hash", "learner_sessions", ["token_hash"], unique=True
    )
    op.create_index("ix_learner_sessions_learner_id", "learner_sessions", ["learner_id"])


def downgrade() -> None:
    op.drop_index("ix_learner_sessions_learner_id", table_name="learner_sessions")
    op.drop_index("ix_learner_sessions_token_hash", table_name="learner_sessions")
    op.drop_table("learner_sessions")
    op.drop_constraint("ck_learners_password_requires_email", "learners", type_="check")
    op.drop_index("ix_learners_email", table_name="learners")
    op.drop_column("learners", "password_hash")
    op.drop_column("learners", "email")
