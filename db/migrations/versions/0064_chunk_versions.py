"""Chunk versions: which pipeline wrote a chunk, and whether a newer one replaced it (S29, S50).

Every existing chunk was written by the only pipeline there has been, so version 1 is a fact
about it, not a guess — a server default is right here. ``superseded_at`` starts NULL: nothing
has been replaced yet. ``embedding`` becomes nullable because a superseded chunk keeps its text
for the citations that point at it and gives up the vector nobody will search again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_chunk_versions"
down_revision: str | Sequence[str] | None = "0063_source_scope_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "chunks",
        sa.Column("pipeline_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("chunks", "embedding", nullable=True)


def downgrade() -> None:
    # A superseded chunk has no vector and cannot satisfy NOT NULL again; it is history nothing
    # can search, so it goes.
    op.execute("DELETE FROM chunks WHERE embedding IS NULL")
    op.alter_column("chunks", "embedding", nullable=False)
    op.drop_column("chunks", "pipeline_version")
    op.drop_column("chunks", "superseded_at")
