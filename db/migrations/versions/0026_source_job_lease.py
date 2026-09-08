"""source ingestion claim: attempts + lease

Gives ingestion a claimable job state (S37). ``attempts`` bounds how many times a source may
be taken, so one that kills its worker every time is eventually parked rather than cycling;
``lease_expires_at`` is what separates "a worker is on this" from "a worker died on this",
which PROCESSING alone could never say.

Existing PROCESSING rows are left with a NULL lease, which reads as immediately claimable —
correct, because nothing survives the deploy still working on them.

Revision ID: 0026_source_job_lease
Revises: 0025_embedding_space
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_source_job_lease"
down_revision: str | Sequence[str] | None = "0025_embedding_space"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("sources", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.alter_column("sources", "attempts", server_default=None)
    op.create_index("ix_sources_status_lease", "sources", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sources_status_lease", table_name="sources")
    op.drop_column("sources", "lease_expires_at")
    op.drop_column("sources", "attempts")
