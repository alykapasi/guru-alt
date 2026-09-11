"""a hash of what a source says, not of the bytes that carry it

An EPUB, a DOCX and a clean PDF of one edition share almost every word and no bytes at all, so
the content hash added in 0033 sees three unrelated files. ``text_sha256`` is the digest of the
canonical extracted text (see ``app/rag/textnorm.py``): container quirks — ligatures, smart
quotes, line-break hyphenation, case, whitespace — and British/American spelling folded away.

Not backfilled. The value only exists after extraction, and recomputing it for existing rows
would mean re-running the pipeline over every source already ingested to save the cost of
ingesting them, which is the wrong way round. Existing sources gain one on their next
re-ingest and are matched against from then on.

Revision ID: 0034_source_text_hash
Revises: 0033_source_content_hash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_source_text_hash"
down_revision: str | Sequence[str] | None = "0033_source_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("text_sha256", sa.String(), nullable=True))
    op.create_index("ix_sources_text_sha256", "sources", ["text_sha256"])


def downgrade() -> None:
    op.drop_index("ix_sources_text_sha256", table_name="sources")
    op.drop_column("sources", "text_sha256")
