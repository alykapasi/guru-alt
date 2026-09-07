"""What one learner may spend before a turn is refused.

Output caps and tool-iteration limits already existed, but nothing bounded what a learner
could accumulate: every turn forwarded the whole conversation, message length had a minimum
and no maximum, and no total was ever compared against anything. A single learner could run
the bill up without any part of the system objecting.

The ceiling is enforced on **both** spend and tokens, because neither subsumes the other: a
model with no price entry contributes nothing to the cost total (``llm_calls.cost_usd`` is
NULL — see ``app/llm/pricing.py``), and a token count says nothing about how expensive the
model producing it was. A budget on cost alone would be silently unenforced for exactly the
models we know least about.

This reads the accounting log rather than a separate counter, which is only trustworthy
because that log now survives the transactions it accompanies (S48). Enforcement is a
pre-turn check, so a single turn can still overshoot the ceiling; it bounds accumulation
across turns, not the cost of any one turn, which output caps already bound.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import LLMCall

WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class Spend:
    """What a learner has used in the trailing window."""

    cost_usd: float
    tokens: int


class BudgetExceeded(Exception):
    """A learner has used their allowance for the window."""

    def __init__(self, message: str, *, spend: Spend) -> None:
        super().__init__(message)
        self.spend = spend


async def spend_since(
    session: AsyncSession, learner_id: uuid.UUID, *, window: timedelta = WINDOW
) -> Spend:
    """This learner's recorded cost and tokens over the trailing ``window``."""
    # llm_calls.created_at is tz-naive (server_default=func.now()), so the bound must be too —
    # the same convention as app/services/analytics.py.
    since = (datetime.now(UTC) - window).replace(tzinfo=None)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                func.coalesce(
                    func.sum(LLMCall.input_tokens + LLMCall.output_tokens),
                    0,
                ),
            ).where(LLMCall.learner_id == learner_id, LLMCall.created_at >= since)
        )
    ).one()
    return Spend(cost_usd=float(row[0]), tokens=int(row[1]))


async def require_budget(session: AsyncSession, learner_id: uuid.UUID, settings: Settings) -> Spend:
    """Raise :class:`BudgetExceeded` if this learner is already over either ceiling."""
    spend = await spend_since(session, learner_id)
    if settings.learner_daily_cost_usd_limit and (
        spend.cost_usd >= settings.learner_daily_cost_usd_limit
    ):
        raise BudgetExceeded(
            f"daily spend limit reached (${spend.cost_usd:.2f} of "
            f"${settings.learner_daily_cost_usd_limit:.2f})",
            spend=spend,
        )
    if settings.learner_daily_token_limit and spend.tokens >= settings.learner_daily_token_limit:
        raise BudgetExceeded(
            f"daily token limit reached ({spend.tokens} of {settings.learner_daily_token_limit})",
            spend=spend,
        )
    return spend
