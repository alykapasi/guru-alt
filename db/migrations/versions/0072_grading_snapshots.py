"""Grading provenance (S56): frozen copies of what each grade was measured against.

Content-addressed and per learner: ``(learner_id, sha256)`` is the key, so an unchanged item
is stored once however often it is graded, and erasing the learner erases their copies. No
foreign key to items or rubrics — deleting either must not take a past grade's explanation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0072_grading_snapshots"
down_revision: str | Sequence[str] | None = "0071_call_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grading_snapshots",
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("grading_snapshots")
