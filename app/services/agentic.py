"""The agentic turn: a bounded tool-calling action, mirroring run_tutor_turn's persistence
shape (app/services/chat.py) but running app/agent/agentic.py's graph instead.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agentic import AgenticState, build_agentic_graph
from app.agent.tools import build_tools
from app.core.config import get_settings
from app.llm.pricing import cost_usd
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, ToolCall, Usage
from app.models.chat import Message
from app.services.turn_common import TurnEvent, add_message, record_llm_call, to_chat_messages

log = structlog.get_logger(__name__)

AGENTIC_SYSTEM_PROMPT = (
    "You are Guru, acting on a specific request. Use the available tools when they would "
    "help answer accurately, then give a clear, concise final answer."
)


async def run_agentic_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    history: Sequence[Message],
    user_content: str,
    max_tokens: int,
    subject_id: uuid.UUID | None = None,
) -> AsyncIterator[TurnEvent]:
    """Persist the user turn, run the bounded tool-calling graph, then persist the reply.

    Intra-loop tool-call/tool-result exchanges are not persisted as ``Message`` rows this
    slice — only the user message and the final assistant reply are, exactly like
    ``run_tutor_turn``. ``tool_call`` events stream in-process for the SSE frame only.
    """
    messages = to_chat_messages(history)
    messages.append(ChatMessage(role=ChatRole.USER, content=user_content))
    await add_message(session, conversation_id, ChatRole.USER.value, user_content)
    await session.commit()

    tools = build_tools(session, llm, learner_id=learner_id, subject_id=subject_id)
    spec = llm.spec(ModelRole.SMART)
    initial: AgenticState = {
        "messages": messages,
        "system": AGENTIC_SYSTEM_PROMPT,
        "max_tokens": max_tokens,
        "max_iterations": get_settings().agentic_max_iterations,
        "iterations": 0,
        "reply": "",
        "usage": Usage(),
        "pending_tool_calls": [],
        "tool_events": [],
    }

    reply = ""
    usage = Usage()
    pending_tool_calls: list[ToolCall] = []
    try:
        async for mode, payload in build_agentic_graph(llm, tools).astream(
            initial, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                if "token" in payload:
                    yield TurnEvent(type="token", text=payload["token"])  # ty: ignore[invalid-argument-type]
                elif "tool_call" in payload:
                    yield TurnEvent(type="tool_call", detail=payload["tool_call"])  # ty: ignore[invalid-argument-type]
            elif mode == "values":
                reply = payload["reply"]  # ty: ignore[invalid-argument-type]
                usage = payload["usage"]  # ty: ignore[invalid-argument-type]
                pending_tool_calls = payload["pending_tool_calls"]  # ty: ignore[invalid-argument-type]
    except Exception as exc:
        log.error("agentic.stream_failed", error=str(exc), model=spec.model)
        yield TurnEvent(type="error", detail="generation failed")
        return

    if pending_tool_calls:
        # Hit max_iterations with the model still mid tool-call — degrade gracefully rather
        # than error, matching the refinement gate's auto-commit-at-max_rounds precedent.
        log.warning("agentic.iteration_cap_hit", conversation_id=str(conversation_id))

    assistant = await add_message(
        session, conversation_id, ChatRole.ASSISTANT.value, reply, model=spec.model
    )
    cost = cost_usd(spec.model, usage)
    await record_llm_call(
        session,
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=ModelRole.SMART.value,
        provider=spec.provider,
        model=spec.model,
        usage=usage,
        cost_usd=cost,
    )
    await session.commit()
    log.info(
        "llm.call",
        role=ModelRole.SMART.value,
        provider=spec.provider,
        model=spec.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )
    yield TurnEvent(
        type="done",
        message_id=str(assistant.id),
        usage=usage,
        cost_usd=cost,
        detail="capped" if pending_tool_calls else "",
    )
