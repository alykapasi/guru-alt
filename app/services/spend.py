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

**Latency is reported here rather than somewhere of its own (P10)**, because it is the other
half of the same row and the question an operator asks needs both: a turn that felt bad is
either a slow model or a slow product, and only cost and time together say which. Two separate
windows over the same table could also disagree with each other about a call that landed
between them, which is a way to lose an afternoon.

Each timing reports the population it was computed over, for the same reason ``unpriced_calls``
exists. A completion carries ``latency_ms`` and a stream carries ``first_token_ms``, so a
percentile over either is a percentile over *some* of the traffic, and one printed without its
count is a number that looks like it describes everything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import LLMCall


class Latency(BaseModel):
    """One timing's percentiles, and how many calls they were computed over.

    ``calls`` is not decoration. A completion records ``latency_ms`` and a stream records
    ``first_token_ms``, so each of these covers part of the traffic; a p95 shown without the
    population it came from reads as a statement about everything. Zero calls means no
    percentile rather than a zero one, which is why both are nullable.
    """

    calls: int
    p50_ms: int | None
    p95_ms: int | None


class SpendBucket(BaseModel):
    """Spend and timing attributed to one role or one model."""

    name: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    unpriced_calls: int
    # How long the provider took, end to end, on the calls that have an end (P10).
    completion: Latency
    # How long a streamed call took to produce its first token — the tutoring turn and the
    # refinement gate, i.e. the calls somebody is waiting through.
    first_token: Latency


class SpendWindow(BaseModel):
    """Total model spend and timing over a window, and whether spend is over budget."""

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
    completion: Latency
    first_token: Latency
    by_role: list[SpendBucket]
    by_model: list[SpendBucket]


def _timing_columns(column) -> list:
    """The three numbers one timing needs: how many calls carry it, then p50 and p95.

    ``percentile_cont`` skips NULLs, which is what makes the count a separate question rather
    than the same one — the percentile is over the rows that have the timing, and the count is
    how many that was. Interpolating (``_cont``) rather than picking an existing row
    (``_disc``) because these are durations, where a value between two observations is a
    perfectly sensible duration.
    """
    return [
        func.count(column),
        func.percentile_cont(0.5).within_group(column.asc()),
        func.percentile_cont(0.95).within_group(column.asc()),
    ]


def _latency(calls: int, p50: float | None, p95: float | None) -> Latency:
    """Round to whole milliseconds; a fractional millisecond is not a fact about a network."""
    return Latency(
        calls=calls,
        p50_ms=None if p50 is None else round(p50),
        p95_ms=None if p95 is None else round(p95),
    )


_SPEND_COLUMNS = [
    func.count().label("calls"),
    func.coalesce(func.sum(LLMCall.input_tokens), 0),
    func.coalesce(func.sum(LLMCall.output_tokens), 0),
    func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
    func.count().filter(LLMCall.cost_usd.is_(None)),
    *_timing_columns(LLMCall.latency_ms),
    *_timing_columns(LLMCall.first_token_ms),
]


async def _buckets(session: AsyncSession, column, since: datetime) -> list[SpendBucket]:
    rows = await session.execute(
        select(column, *_SPEND_COLUMNS)
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
            completion=_latency(timed, p50, p95),
            first_token=_latency(streamed, first_p50, first_p95),
        )
        for (
            name,
            calls,
            input_tokens,
            output_tokens,
            cost,
            unpriced,
            timed,
            p50,
            p95,
            streamed,
            first_p50,
            first_p95,
        ) in rows
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
        await session.execute(select(*_SPEND_COLUMNS).where(LLMCall.created_at >= since))
    ).one()
    (
        calls,
        input_tokens,
        output_tokens,
        cost,
        unpriced,
        timed,
        p50,
        p95,
        streamed,
        first_p50,
        first_p95,
    ) = totals
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
        completion=_latency(timed, p50, p95),
        first_token=_latency(streamed, first_p50, first_p95),
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
