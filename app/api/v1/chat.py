"""Chat endpoints: conversations + an SSE streaming tutor turn."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.core.config import get_settings
from app.schemas.chat import (
    ChatTurnRequest,
    ConversationCreate,
    ConversationRead,
    MessageRead,
)
from app.services import chat as svc

router = APIRouter(tags=["chat"])


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
    max_tokens = get_settings().chat_max_tokens

    async def event_stream() -> AsyncIterator[str]:
        async for ev in svc.run_tutor_turn(
            session,
            llm,
            learner_id=learner.id,
            conversation_id=conversation_id,
            history=history,
            user_content=data.content,
            max_tokens=max_tokens,
        ):
            if ev.type == "token":
                yield _sse({"type": "token", "text": ev.text})
            elif ev.type == "error":
                yield _sse({"type": "error", "detail": ev.detail})
            elif ev.type == "done":
                yield _sse(
                    {
                        "type": "done",
                        "message_id": ev.message_id,
                        "usage": {
                            "input_tokens": ev.usage.input_tokens,
                            "output_tokens": ev.usage.output_tokens,
                        },
                        "cost_usd": ev.cost_usd,
                    }
                )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
