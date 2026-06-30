"""Chat endpoints: conversations + an SSE streaming tutor turn."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.core.config import get_settings
from app.llm.pricing import cost_usd
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.schemas.chat import (
    ChatTurnRequest,
    ConversationCreate,
    ConversationRead,
    MessageRead,
)
from app.services import chat as svc

router = APIRouter(tags=["chat"])
log = structlog.get_logger(__name__)


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    data: ConversationCreate, session: SessionDep, learner: CurrentLearner
):
    return await svc.create_conversation(session, learner.id, data.title)


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(session: SessionDep, learner: CurrentLearner):
    return await svc.list_conversations(session, learner.id)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return await svc.list_messages(session, conversation_id)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    data: ChatTurnRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
) -> StreamingResponse:
    """Persist the user turn, then stream the tutor's reply as Server-Sent Events."""
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    history = await svc.list_messages(session, conversation_id)
    chat_messages = svc.to_chat_messages(history)
    chat_messages.append(ChatMessage(role=ChatRole.USER, content=data.content))
    await svc.add_message(session, conversation_id, ChatRole.USER.value, data.content)
    await session.commit()

    spec = llm.spec(ModelRole.SMART)
    max_tokens = get_settings().chat_max_tokens

    async def event_stream() -> AsyncIterator[str]:
        parts: list[str] = []
        usage = Usage()
        try:
            async for chunk in llm.stream(
                ModelRole.SMART,
                chat_messages,
                system=svc.TUTOR_SYSTEM_PROMPT,
                max_tokens=max_tokens,
            ):
                if chunk.text:
                    parts.append(chunk.text)
                    yield _sse({"type": "token", "text": chunk.text})
                if chunk.usage is not None:
                    usage = chunk.usage
        except Exception as exc:
            log.error("chat.stream_failed", error=str(exc), model=spec.model)
            yield _sse({"type": "error", "detail": "generation failed"})
            return

        content = "".join(parts)
        assistant = await svc.add_message(
            session, conversation_id, ChatRole.ASSISTANT.value, content, model=spec.model
        )
        cost = cost_usd(spec.model, usage)
        await svc.record_llm_call(
            session,
            learner_id=learner.id,
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
        yield _sse(
            {
                "type": "done",
                "message_id": str(assistant.id),
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
                "cost_usd": cost,
            }
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
