"""The one place a model call becomes a record (CLAUDE.md: token/cost per call, day one).

Two properties this module exists to hold:

**Accounting is not part of the work it pays for.** The row is written on its own session
and committed immediately, so a business transaction that rolls back after a paid call
still leaves the call recorded. The money left regardless of whether the work survived.

**Every call is also emitted as a structured log**, unconditionally, so the record exists
even if the database write is the thing that fails.

Tests rebind the session factory (see ``tests/conftest.py``) so accounting shares the
test's transaction and rolls back with it.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory
from app.llm.pricing import price_usd
from app.llm.registry import ModelSpec
from app.llm.types import Usage
from app.models.chat import LLMCall

log = structlog.get_logger(__name__)

AccountingSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_session_factory: AccountingSessionFactory = SessionFactory


def set_accounting_session_factory(factory: AccountingSessionFactory) -> AccountingSessionFactory:
    """Point accounting at a different session source. Returns the previous one."""
    global _session_factory
    previous = _session_factory
    _session_factory = factory
    return previous


async def log_llm_call(
    *,
    learner_id: uuid.UUID | None,
    role: str,
    spec: ModelSpec,
    usage: Usage,
    conversation_id: uuid.UUID | None = None,
) -> float | None:
    """Record one call's tokens + estimated cost. Returns the cost, or ``None`` if unpriced.

    Commits on its own transaction; the caller's session is untouched.
    """
    cost = price_usd(spec.provider, spec.model, usage)
    log.info(
        "llm.call",
        role=role,
        provider=spec.provider,
        model=spec.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )
    async with _session_factory() as session:
        session.add(
            LLMCall(
                learner_id=learner_id,
                conversation_id=conversation_id,
                role=role,
                provider=spec.provider,
                model=spec.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost,
            )
        )
        await session.commit()
    return cost
