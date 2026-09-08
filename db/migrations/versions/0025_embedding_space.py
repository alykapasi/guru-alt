"""Record which embedding model produced each stored vector.

Vectors are only comparable to other vectors from the same model. Nothing recorded which
model produced a stored one, so changing ``GURU_MODEL_EMBED`` to a different model of the
same dimension — a one-line config edit, no schema change, no error — left the database full
of vectors that cosine distance would rank against queries from an unrelated space. The
results would be wrong and would look entirely normal.

Existing rows are backfilled with the currently configured space. That is an assertion about
history, and it is true exactly if the EMBED model has not changed since those rows were
written. If it has, those rows are mislabelled and the remedy is to re-ingest the affected
sources; there is no way to recover the truth from the vectors themselves.

Revision ID: 0025_embedding_space
Revises: 0024_llm_call_price_unknown
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings
from app.llm.registry import _parse_spec

revision: str = "0025_embedding_space"
down_revision: str | Sequence[str] | None = "0024_llm_call_price_unknown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("chunks", "memories")


def _current_space() -> str:
    settings = get_settings()
    spec = _parse_spec(settings.model_embed)
    return f"{spec.provider}:{spec.model}:{settings.embed_dim}"


def upgrade() -> None:
    space = _current_space()
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("embedding_space", sa.String(), nullable=False, server_default=space),
        )
        # The default exists only to fill existing rows; writers always supply the value, and
        # leaving it would silently label future rows with today's model.
        op.alter_column(table, "embedding_space", server_default=None)
        op.create_index(f"ix_{table}_embedding_space", table, ["embedding_space"])


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f"ix_{table}_embedding_space", table_name=table)
        op.drop_column(table, "embedding_space")
