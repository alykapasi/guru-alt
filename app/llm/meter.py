"""Every model call is recorded by the client, before and after (S48), and admitted first (S47).

Before: the spend guard (``ADMIT``, installed by ``app.services.spend_guard``) may refuse the
call, and a ``pending`` row is written carrying a reserved estimate — so a call that never
comes back still counts, and concurrent calls see each other. After: the row is settled as
``ok`` with the provider's numbers, ``failed`` with the exception's class name (its message may
contain learner text), or ``partial`` for a stream closed before its last chunk.

Accounting is written on its own session and never fails the work: a failed write is logged
and the call proceeds. ``BudgetExceeded`` is the one exception raised here on purpose.
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import update

from app.core.config import get_settings
from app.llm.attribution import Attribution, current
from app.llm.pricing import price_usd
from app.llm.types import Usage

log = structlog.get_logger(__name__)

Scope = Literal["learner", "deployment"]
MESSAGES: dict[str, str] = {
    "learner": "You've reached today's usage limit. It resets over the next 24 hours.",
    "deployment": "Guru has reached its usage limit for now — try again later.",
}


class BudgetExceeded(Exception):
    """A paid call refused before it was made (S47)."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(MESSAGES[scope])
        self.scope: Scope = scope
        self.message = MESSAGES[scope]


@dataclass(frozen=True)
class Estimate:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


# (session, attribution, estimate) -> None, raising BudgetExceeded. Installed by
# app.services.spend_guard at import; None admits everything (a process that never imported
# the guard, e.g. a bare script).
Admit = Callable[..., Awaitable[None]]
ADMIT: Admit | None = None


@dataclass(frozen=True)
class Reservation:
    row_id: uuid.UUID | None
    provider: str
    model: str
    input_tokens: int


def _hash(system: str | None) -> str | None:
    return hashlib.sha256(system.encode()).hexdigest()[:16] if system else None


def _estimate(provider: str, model: str, *, input_chars: int, max_output: int) -> Estimate:
    input_tokens = max(1, input_chars // 4)
    usage = Usage(input_tokens=input_tokens, output_tokens=max_output)
    return Estimate(input_tokens, max_output, price_usd(provider, model, usage))


async def open_call(
    role: str,
    provider: str,
    model: str,
    *,
    system: str | None,
    input_chars: int,
    max_output: int,
) -> Reservation:
    """Admit the call and write its pending row. Raises ``BudgetExceeded`` when refused."""
    from app.models.chat import LLMCall
    from app.services.llm_log import accounting_session

    who: Attribution = current()
    estimate = _estimate(provider, model, input_chars=input_chars, max_output=max_output)
    row_id = uuid.uuid4()
    try:
        async with accounting_session() as session:
            if ADMIT is not None:
                await ADMIT(session, who, estimate)
            session.add(
                LLMCall(
                    id=row_id,
                    learner_id=who.learner_id,
                    conversation_id=who.conversation_id,
                    role=role,
                    provider=provider,
                    model=model,
                    input_tokens=estimate.input_tokens,
                    output_tokens=estimate.output_tokens,
                    cost_usd=estimate.cost_usd,
                    feature=who.feature or "unattributed",
                    request_id=who.request_id,
                    status="pending",
                    estimated=True,
                    prompt_hash=_hash(system),
                    app_version=get_settings().app_version,
                )
            )
            await session.commit()
    except BudgetExceeded:
        raise
    except Exception as exc:  # bookkeeping must not become an outage
        log.error("llm.call_not_recorded", role=role, model=model, error=type(exc).__name__)
        return Reservation(None, provider, model, estimate.input_tokens)
    return Reservation(row_id, provider, model, estimate.input_tokens)


async def _update(reservation: Reservation, **values: object) -> None:
    from app.models.chat import LLMCall
    from app.services.llm_log import accounting_session

    if reservation.row_id is None:
        return
    try:
        async with accounting_session() as session:
            await session.execute(
                update(LLMCall).where(LLMCall.id == reservation.row_id).values(**values)
            )
            await session.commit()
    except Exception as exc:
        log.error("llm.call_not_recorded", model=reservation.model, error=type(exc).__name__)


async def settle(reservation: Reservation, usage: Usage) -> None:
    cost = price_usd(reservation.provider, reservation.model, usage)
    log.info(
        "llm.call",
        model=reservation.model,
        status="ok",
        feature=current().feature,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        latency_ms=usage.latency_ms,
        first_token_ms=usage.first_token_ms,
    )
    await _update(
        reservation,
        status="ok",
        estimated=False,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        latency_ms=usage.latency_ms,
        first_token_ms=usage.first_token_ms,
    )


async def fail(reservation: Reservation, exc: BaseException) -> None:
    usage = Usage(input_tokens=reservation.input_tokens, output_tokens=0)
    log.info("llm.call", model=reservation.model, status="failed", error_kind=type(exc).__name__)
    await _update(
        reservation,
        status="failed",
        error_kind=type(exc).__name__,
        estimated=True,
        input_tokens=usage.input_tokens,
        output_tokens=0,
        cost_usd=price_usd(reservation.provider, reservation.model, usage),
    )


async def partial(reservation: Reservation, delivered_chars: int) -> None:
    usage = Usage(input_tokens=reservation.input_tokens, output_tokens=max(0, delivered_chars // 4))
    log.info("llm.call", model=reservation.model, status="partial")
    await _update(
        reservation,
        status="partial",
        estimated=True,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=price_usd(reservation.provider, reservation.model, usage),
    )
