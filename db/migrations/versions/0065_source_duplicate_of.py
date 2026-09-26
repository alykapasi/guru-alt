"""sources.duplicate_of_id: the text-duplicate relation as a foreign key (S77).

It lived in ``meta.duplicate_of``, where nothing noticed when the original went away. As a
foreign key with ON DELETE SET NULL, a deleted original leaves a NULL the recovery sweep can
find. Backfilled from meta where the named original still exists; an original that is already
gone backfills to NULL, which is exactly the stranded state the sweep recovers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_source_duplicate_of"
down_revision: str | Sequence[str] | None = "0064_chunk_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "duplicate_of_id",
            sa.Uuid(),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_sources_duplicate_of_id", "sources", ["duplicate_of_id"])
    op.execute(
        """
        UPDATE sources AS d SET duplicate_of_id = o.id
          FROM sources AS o
         WHERE d.meta ? 'duplicate_of'
           AND o.id::text = d.meta->>'duplicate_of'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_sources_duplicate_of_id", table_name="sources")
    op.drop_column("sources", "duplicate_of_id")
