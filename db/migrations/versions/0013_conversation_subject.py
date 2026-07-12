"""conversation_subject

Revision ID: 0013_conversation_subject
Revises: 0012_lesson_plans
Create Date: 2026-07-12 00:00:00.000000

Phase 5. Scopes a conversation to a subject (nullable, set once at creation) so tutor-turn
plan grounding and the session runner's practice item can look up an exact
(learner, subject) lesson plan instead of the prior cross-subject "most recently updated"
heuristic. NULL for subject-less/freeform conversations and all pre-existing rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_conversation_subject"
down_revision: str | Sequence[str] | None = "0012_lesson_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("conversations", sa.Column("subject_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_conversations_subject_id"), "conversations", ["subject_id"], unique=False
    )
    op.create_foreign_key(
        "fk_conversations_subject_id_subjects",
        "conversations",
        "subjects",
        ["subject_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_conversations_subject_id_subjects", "conversations", type_="foreignkey")
    op.drop_index(op.f("ix_conversations_subject_id"), table_name="conversations")
    op.drop_column("conversations", "subject_id")
