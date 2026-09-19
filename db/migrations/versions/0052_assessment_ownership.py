"""Private assessment ownership; unknown historical generation stays quarantined."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_assessment_ownership"
down_revision: str | Sequence[str] | None = "0051_message_admin_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("items", "rubrics"):
        op.add_column(
            table, sa.Column("visibility", sa.String(), nullable=False, server_default="private")
        )
        op.add_column(table, sa.Column("owner_learner_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_owner_learner_id",
            table,
            "learners",
            ["owner_learner_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(f"ix_{table}_visibility", table, ["visibility"])
        op.create_index(f"ix_{table}_owner_learner_id", table, ["owner_learner_id"])
    op.execute(
        "UPDATE items SET owner_learner_id = author_learner_id WHERE author_learner_id IS NOT NULL"
    )
    # A unique private graph owner is positive provenance; usage by a learner is not.
    op.execute("""
        UPDATE items i SET owner_learner_id = ownership.owner FROM (
          SELECT ik.item_id, min(s.owner_learner_id::text)::uuid AS owner
          FROM item_kcs ik JOIN kcs k ON k.id=ik.kc_id
          JOIN topics t ON t.id=k.topic_id JOIN subjects s ON s.id=t.subject_id
          GROUP BY ik.item_id HAVING count(*) = count(s.owner_learner_id)
            AND count(DISTINCT s.owner_learner_id) = 1
        ) ownership WHERE i.id=ownership.item_id AND i.owner_learner_id IS NULL
    """)
    op.execute("""
        UPDATE rubrics r SET owner_learner_id=s.owner_learner_id
        FROM kcs k JOIN topics t ON t.id=k.topic_id JOIN subjects s ON s.id=t.subject_id
        WHERE r.kc_id=k.id AND s.owner_learner_id IS NOT NULL
    """)
    op.execute("""
        UPDATE rubrics r SET owner_learner_id=ownership.owner FROM (
          SELECT rubric_id, min(owner_learner_id::text)::uuid AS owner FROM items
          WHERE rubric_id IS NOT NULL GROUP BY rubric_id
          HAVING count(*)=count(owner_learner_id) AND count(DISTINCT owner_learner_id)=1
        ) ownership WHERE r.id=ownership.rubric_id AND r.owner_learner_id IS NULL
    """)


def downgrade() -> None:
    for table in ("rubrics", "items"):
        op.drop_index(f"ix_{table}_owner_learner_id", table_name=table)
        op.drop_index(f"ix_{table}_visibility", table_name=table)
        op.drop_constraint(f"fk_{table}_owner_learner_id", table, type_="foreignkey")
        op.drop_column(table, "owner_learner_id")
        op.drop_column(table, "visibility")
