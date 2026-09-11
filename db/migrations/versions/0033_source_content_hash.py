"""content-addressed blobs and a queryable content hash

The SHA-256 of an upload was already being computed — to build the object key — and then
thrown away inside a path prefixed with the learner and source ids. A digest buried in a
string cannot answer "do I already have this file?", so nothing did.

``content_sha256`` lifts it into a column. New uploads are also keyed by content alone
(``blobs/<sha>``), so two learners uploading the same bytes reference one stored object.

Existing rows keep their old ``{learner}/{source}/{sha}`` keys, which still resolve: the code
uses whatever the row holds. They are not backfilled or re-keyed, because that would mean
copying every object in the store to save space on files already stored. They simply do not
share, and no future upload of the same bytes will share with them either.

Revision ID: 0033_source_content_hash
Revises: 0032_item_authorship
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_source_content_hash"
down_revision: str | Sequence[str] | None = "0032_item_authorship"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("content_sha256", sa.String(), nullable=True))
    op.create_index("ix_sources_content_sha256", "sources", ["content_sha256"])
    # Reference lookup on delete: "does any surviving source still point at these bytes?"
    op.create_index("ix_sources_blob_key", "sources", ["blob_key"])


def downgrade() -> None:
    op.drop_index("ix_sources_blob_key", table_name="sources")
    op.drop_index("ix_sources_content_sha256", table_name="sources")
    op.drop_column("sources", "content_sha256")
