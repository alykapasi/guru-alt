"""Let an LLM call record that its price is unknown, rather than reporting it as free.

``cost_usd`` was NOT NULL DEFAULT 0.0, so a model missing from the price table was written
as costing nothing — indistinguishable from a locally hosted model that genuinely costs
nothing. A spend total that should have read "we do not know" read "zero".

NULL now means "no known price for this model". Historical rows are left at 0.0: we cannot
tell retrospectively which of them were unpriced and which were free, and inventing that
distinction would be worse than leaving it absent.

Revision ID: 0024_llm_call_price_unknown
Revises: 0023_plan_objective_kc_ids
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_llm_call_price_unknown"
down_revision: str | Sequence[str] | None = "0023_plan_objective_kc_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("llm_calls", "cost_usd", existing_type=sa.Double(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE llm_calls SET cost_usd = 0.0 WHERE cost_usd IS NULL")
    op.alter_column("llm_calls", "cost_usd", existing_type=sa.Double(), nullable=False)
