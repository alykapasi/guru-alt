"""Concept links, learner decisions on them, and transfer columns on learner_kc_state (S24).

A link is a claim that two presentations in different subjects are the same idea, endorsed by
an administrator (curated pairs) or the LLM judge (pairs touching a learner's own material),
and in effect for a learner only once that learner accepts it. The three state columns record
a head start carried over such a link, which stays provisional until confirmed.

No backfill: nothing has ever been linked.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_concept_links"
down_revision: str | Sequence[str] | None = "0060_practice_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "concept_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "kc_a_id", sa.Uuid(), sa.ForeignKey("kcs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "kc_b_id", sa.Uuid(), sa.ForeignKey("kcs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column(
            "owner_learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("verdict", sa.String(), nullable=True),
        sa.Column("endorsed_by", sa.String(), nullable=True),
        sa.Column(
            "decided_by_admin_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("kc_a_id", "kc_b_id"),
        sa.CheckConstraint("kc_a_id < kc_b_id", name="ck_concept_links_ordered"),
    )
    op.create_index("ix_concept_links_kc_a_id", "concept_links", ["kc_a_id"])
    op.create_index("ix_concept_links_kc_b_id", "concept_links", ["kc_b_id"])
    op.create_index("ix_concept_links_owner_learner_id", "concept_links", ["owner_learner_id"])
    op.create_table(
        "concept_link_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "link_id",
            sa.Uuid(),
            sa.ForeignKey("concept_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("learner_id", "link_id"),
    )
    op.create_index(
        "ix_concept_link_decisions_learner_id", "concept_link_decisions", ["learner_id"]
    )
    op.create_index("ix_concept_link_decisions_link_id", "concept_link_decisions", ["link_id"])
    op.add_column(
        "learner_kc_state",
        sa.Column(
            "transferred_from_kc_id",
            sa.Uuid(),
            sa.ForeignKey("kcs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "learner_kc_state",
        sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "learner_kc_state",
        sa.Column("transfer_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("learner_kc_state", "transfer_confirmed_at")
    op.drop_column("learner_kc_state", "transferred_at")
    op.drop_column("learner_kc_state", "transferred_from_kc_id")
    op.drop_table("concept_link_decisions")
    op.drop_table("concept_links")
