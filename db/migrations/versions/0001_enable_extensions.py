"""enable required postgres extensions

Enables pgvector (embeddings) and pg_trgm (fuzzy/keyword search). These underpin
the hybrid retrieval layer (Phase 4) and must exist before any vector/trigram
columns or indexes are created.

Revision ID: 0001_enable_extensions
Revises:
Create Date: 2026-06-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_enable_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
