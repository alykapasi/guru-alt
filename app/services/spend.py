"""What this deployment has spent, over a window (S60).

Cost has been recorded per call since Phase 1 and capped per learner since S47. What nothing
did was look at the *total* — so the failure mode was a bill, discovered monthly, with no
signal in between. This is the read side of that: one query over ``llm_calls``, grouped the
two ways an operator acts on (which role, which model), against a configured budget.

The one number that is not what it looks like is the total. ``cost_usd`` is nullable and NULL
means "this model has no known price" — deliberately distinct from 0.0, which means it ran
locally and cost nothing (S48). Summing NULLs as zero would report a deployment running
entirely on unpriced models as spending nothing at all, which is the most expensive kind of
wrong. So the unpriced calls are counted and reported alongside, and a reader who sees a
non-zero ``unpriced_calls`` knows the total is a floor rather than a figure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import LLMCall


class SpendBucket(BaseModel):
    """Spend attributed to one role or one model."""

    name: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    unpriced_calls: int


class SpendWindow(BaseModel):
    """Total model spend over a window, and whether it is over budget."""

    window_hours: int
    since: datetime
    calls: int
    input_tokens: int
    output_tokens: int
    # A floor, not a figure, whenever ``unpriced_calls`` is non-zero — see the module docstring.
    cost_usd: float
    unpriced_calls: int
    budget_usd: float | None
    over_budget: bool
    by_role: list[SpendBucket]
    by_model: list[SpendBucket]


async def _buckets(session: AsyncSession, column, since: datetime) -> list[SpendBucket]:
    rows = await session.execute(
        select(
            column,
            func.count().label("calls"),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
            func.count().filter(LLMCall.cost_usd.is_(None)),
        )
        .where(LLMCall.created_at >= since)
        .group_by(column)
        .order_by(func.coalesce(func.sum(LLMCall.cost_usd), 0.0).desc())
    )
    return [
        SpendBucket(
            name=str(name),
            calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=float(cost),
            unpriced_calls=unpriced,
        )
        for name, calls, input_tokens, output_tokens, cost, unpriced in rows
    ]


async def window(
    session: AsyncSession, *, settings: Settings, hours: int | None = None
) -> SpendWindow:
    """Spend over the last ``hours`` (default ``settings.spend_window_hours``)."""
    window_hours = hours if hours is not None else settings.spend_window_hours
    # Naive UTC: `llm_calls.created_at` is TIMESTAMP WITHOUT TIME ZONE, and comparing it to an
    # aware value would raise rather than quietly compare wrong.
    since = (datetime.now(UTC) - timedelta(hours=window_hours)).replace(tzinfo=None)

    totals = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(LLMCall.input_tokens), 0),
                func.coalesce(func.sum(LLMCall.output_tokens), 0),
                func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                func.count().filter(LLMCall.cost_usd.is_(None)),
            ).where(LLMCall.created_at >= since)
        )
    ).one()
    calls, input_tokens, output_tokens, cost, unpriced = totals
    budget = settings.spend_budget_usd

    return SpendWindow(
        window_hours=window_hours,
        since=since,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=float(cost),
        unpriced_calls=unpriced,
        budget_usd=budget,
        over_budget=budget is not None and float(cost) > budget,
        by_role=await _buckets(session, LLMCall.role, since),
        by_model=await _buckets(session, LLMCall.model, since),
    )


async def learner_spend(session: AsyncSession, learner_id: uuid.UUID, *, hours: int) -> float:
    """One learner's priced spend over a window. Used by the per-learner cap (S47)."""
    since = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
    total = await session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
            LLMCall.learner_id == learner_id, LLMCall.created_at >= since
        )
    )
    return float(total or 0.0)
