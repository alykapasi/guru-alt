"""What may be spent, checked before every paid call (S47).

The learner's caps are exact, including under concurrency: admission and the pending row are
one accounting transaction holding an advisory lock on the learner, so parallel calls queue for
a moment and each sees the others' reservations. What counts is every row in the trailing 24
hours — settled at its real numbers, pending at its reserved estimate, including a pending row
a crashed process left behind (unknown spend counts until it leaves the window).

The deployment ceiling reads a total cached for ``spend_guard_cache_seconds`` per process:
summing the whole window on every call would cost more than it protects. It can therefore lag
by that long — a soft edge on a hard ceiling; the learner caps stay exact.

Background work (write-back, profile refresh, concept links, reindex) yields first: it is
refused at 90% of either limit, so automatic work never spends what a learner needs for their
own turns, and never takes the deployment to its ceiling.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import meter
from app.llm.attribution import Attribution
from app.llm.meter import BudgetExceeded, Estimate
from app.models.chat import LLMCall
from app.services.llm_log import accounting_session

log = structlog.get_logger(__name__)

WINDOW = timedelta(hours=24)
BACKGROUND_SHARE = 0.9

_cache: tuple[float, float] | None = None  # (monotonic time read, deployment total)


def reset_cache() -> None:
    global _cache
    _cache = None


@dataclass(frozen=True)
class Spend:
    cost_usd: float
    tokens: int


def _since(window: timedelta) -> datetime:
    return (datetime.now(UTC) - window).replace(tzinfo=None)


async def spend_since(
    session: AsyncSession, learner_id: uuid.UUID, *, window: timedelta = WINDOW
) -> Spend:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0),
            ).where(LLMCall.learner_id == learner_id, LLMCall.created_at >= _since(window))
        )
    ).one()
    return Spend(cost_usd=float(row[0]), tokens=int(row[1]))


def _learner_over(spend: Spend, estimate: Estimate | None, *, background: bool) -> bool:
    settings = get_settings()
    share = BACKGROUND_SHARE if background else 1.0
    extra_cost = (estimate.cost_usd or 0.0) if estimate else 0.0
    extra_tokens = (estimate.input_tokens + estimate.output_tokens) if estimate else 0
    cost_limit = settings.learner_daily_cost_usd_limit
    token_limit = settings.learner_daily_token_limit
    if estimate is None:
        # Nothing reserved yet (the preflight): exactly at the cap is already spent.
        over_cost = bool(cost_limit) and spend.cost_usd >= cost_limit * share
        over_tokens = bool(token_limit) and spend.tokens >= token_limit * share
    else:
        over_cost = bool(cost_limit) and spend.cost_usd + extra_cost > cost_limit * share
        over_tokens = bool(token_limit) and spend.tokens + extra_tokens > token_limit * share
    return over_cost or over_tokens


async def _deployment_total(session: AsyncSession) -> float | None:
    global _cache
    settings = get_settings()
    if _cache is not None and time.monotonic() - _cache[0] < settings.spend_guard_cache_seconds:
        return _cache[1]
    try:
        total = float(
            await session.scalar(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                    LLMCall.created_at >= _since(timedelta(hours=settings.spend_window_hours))
                )
            )
            or 0.0
        )
    except Exception:
        log.warning("budget.deployment_total_unreadable")
        return _cache[1] if _cache is not None else None
    _cache = (time.monotonic(), total)
    return total


async def _check_deployment(session: AsyncSession, *, background: bool) -> None:
    budget = get_settings().spend_budget_usd
    if not budget:
        return
    total = await _deployment_total(session)
    if total is None:
        return
    if total >= budget * (BACKGROUND_SHARE if background else 1.0):
        raise BudgetExceeded("deployment")


async def check(
    session: AsyncSession, learner_id: uuid.UUID | None, *, background: bool = False
) -> None:
    """Refuse early (the chat preflight) without reserving anything."""
    await _check_deployment(session, background=background)
    if learner_id is not None and _learner_over(
        await spend_since(session, learner_id), None, background=background
    ):
        raise BudgetExceeded("learner")


async def admit(session: AsyncSession, who: Attribution, estimate: Estimate) -> None:
    """``meter.ADMIT``: runs in the transaction that then writes the pending row."""
    await _check_deployment(session, background=who.background)
    if who.learner_id is None:
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"spend:{who.learner_id}"},
    )
    if _learner_over(
        await spend_since(session, who.learner_id), estimate, background=who.background
    ):
        raise BudgetExceeded("learner")


async def background_paused() -> bool:
    """Whether the deployment is past the share at which background work stops."""
    budget = get_settings().spend_budget_usd
    if not budget:
        return False
    async with accounting_session() as session:
        total = await _deployment_total(session)
    return total is not None and total >= budget * BACKGROUND_SHARE


meter.ADMIT = admit
