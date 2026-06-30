"""Shared per-call LLM cost logging (CLAUDE.md: token/cost logged per call, day one).

One ``LLMCall`` row per model call, tagged by role + resolved model, with estimated cost.
Conversation-scoped chat keeps its own logger; this serves callers with no conversation
(e.g. rubric grading). The caller controls the commit boundary.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.pricing import cost_usd
from app.llm.registry import ModelSpec
from app.llm.types import Usage
from app.models.chat import LLMCall


async def log_llm_call(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID | None,
    role: str,
    spec: ModelSpec,
    usage: Usage,
    conversation_id: uuid.UUID | None = None,
) -> LLMCall:
    """Record one call's tokens + estimated cost. Flushes; the caller commits."""
    call = LLMCall(
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=role,
        provider=spec.provider,
        model=spec.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost_usd(spec.model, usage),
    )
    session.add(call)
    await session.flush()
    return call
