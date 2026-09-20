"""link a learner to their hosted-identity provider subject, and gate enrollment on invitation

S21 moves credentials to a hosted identity provider (Clerk). Three things this schema has to
carry, none of which the provider can be trusted to hold on Guru's behalf:

``learners.auth_subject`` is the provider's id for a person, unique so one sign-in never
resolves to two accounts. ``learners.suspended_at`` lets an administrator stop access without
deleting anything — suspension and deletion are different acts and share no column.

``invitations`` is what actually makes the alpha invite-only. The provider is asked to
*deliver* an invitation, but whether an address may enroll at all is decided here, by a row
this database controls — a misconfigured dashboard toggle on the provider's side cannot turn
the alpha into open registration. The partial unique index on ``email`` allows at most one
*open* invitation (both ``accepted_at`` and ``revoked_at`` NULL) per address at a time, while
letting a spent or withdrawn one stay as history and a fresh invitation follow it.

``account_actions`` is the audit for administrative acts that touch no session — granting or
revoking an invitation, suspending or reinstating an account — the counterpart to
``impersonations``, which audits visits. Both foreign keys are SET NULL and both the actor and
the subject's handle are captured as text beside them, for the same reason ``impersonations``
does it: an audit a subject can erase by closing their account is not an audit of access to
that account, and one an administrator can erase by closing theirs is not an audit of anything.
``app.services.retention`` states, and enforces, which half survives.

Revision ID: 0054_hosted_identity
Revises: 0053_note_exact_authorship
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_hosted_identity"
down_revision: str | Sequence[str] | None = "0053_note_exact_authorship"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("learners", sa.Column("auth_subject", sa.String(), nullable=True))
    op.add_column("learners", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_learners_auth_subject", "learners", ["auth_subject"], unique=True)

    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("invited_by_learner_id", sa.Uuid(), nullable=True),
        sa.Column("invited_by_handle", sa.String(), nullable=False),
        sa.Column("provider_invitation_id", sa.String(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_learner_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_learner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invited_by_learner_id"], ["learners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["accepted_learner_id"], ["learners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["revoked_by_learner_id"], ["learners.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_invitations_email", "invitations", ["email"])
    op.create_index(
        "ix_invitations_invited_by_learner_id", "invitations", ["invited_by_learner_id"]
    )
    op.create_index("ix_invitations_accepted_learner_id", "invitations", ["accepted_learner_id"])
    op.create_index(
        "uq_invitations_open_email",
        "invitations",
        ["email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )

    op.create_table(
        "account_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_learner_id", sa.Uuid(), nullable=True),
        sa.Column("actor_handle", sa.String(), nullable=False),
        sa.Column("learner_id", sa.Uuid(), nullable=True),
        sa.Column("learner_handle", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_learner_id"], ["learners.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_account_actions_actor_learner_id", "account_actions", ["actor_learner_id"])
    op.create_index("ix_account_actions_learner_id", "account_actions", ["learner_id"])
    op.create_index("ix_account_actions_action", "account_actions", ["action"])


def downgrade() -> None:
    op.drop_index("ix_account_actions_action", table_name="account_actions")
    op.drop_index("ix_account_actions_learner_id", table_name="account_actions")
    op.drop_index("ix_account_actions_actor_learner_id", table_name="account_actions")
    op.drop_table("account_actions")

    op.drop_index("uq_invitations_open_email", table_name="invitations")
    op.drop_index("ix_invitations_accepted_learner_id", table_name="invitations")
    op.drop_index("ix_invitations_invited_by_learner_id", table_name="invitations")
    op.drop_index("ix_invitations_email", table_name="invitations")
    op.drop_table("invitations")

    op.drop_index("ix_learners_auth_subject", table_name="learners")
    op.drop_column("learners", "suspended_at")
    op.drop_column("learners", "auth_subject")
