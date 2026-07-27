"""notes

Revision ID: 0017_notes
Revises: 0016_conversation_kind
Create Date: 2026-07-27 00:00:00.000000

Phase 8. ``notes`` (one living note per learner+topic; JSONB atom substrate + watermark),
``note_revisions`` (append-only history), ``note_renders`` (cached per-format projections).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017_notes"
down_revision: str | Sequence[str] | None = "0016_conversation_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notes",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("substrate", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("watermark", sa.DateTime(), nullable=False),
        sa.Column("format", sa.String(), nullable=True),
        sa.Column("revision_ordinal", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learner_id", "topic_id"),
    )
    op.create_index(op.f("ix_notes_learner_id"), "notes", ["learner_id"], unique=False)
    op.create_index(op.f("ix_notes_topic_id"), "notes", ["topic_id"], unique=False)

    op.create_table(
        "note_revisions",
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("substrate", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cause", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "ordinal"),
    )
    op.create_index(op.f("ix_note_revisions_note_id"), "note_revisions", ["note_id"], unique=False)
    op.create_index(
        op.f("ix_note_revisions_created_at"), "note_revisions", ["created_at"], unique=False
    )

    op.create_table(
        "note_renders",
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("revision_ordinal", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "revision_ordinal", "format"),
    )
    op.create_index(op.f("ix_note_renders_note_id"), "note_renders", ["note_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_note_renders_note_id"), table_name="note_renders")
    op.drop_table("note_renders")
    op.drop_index(op.f("ix_note_revisions_created_at"), table_name="note_revisions")
    op.drop_index(op.f("ix_note_revisions_note_id"), table_name="note_revisions")
    op.drop_table("note_revisions")
    op.drop_index(op.f("ix_notes_topic_id"), table_name="notes")
    op.drop_index(op.f("ix_notes_learner_id"), table_name="notes")
    op.drop_table("notes")
