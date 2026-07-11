"""Chat endpoints: conversations + an SSE streaming tutor turn.

``send_message`` dispatches each turn to one of two LangGraph flows: the interactive
refinement gate (while the conversation has no committed ``goal`` yet) or the plain tutor
turn (once a goal is committed, or the gate was never entered). See
``app/services/refinement.py`` for the gate's persistence orchestration and its dispatch
edge cases (e.g. a lost in-memory checkpoint degrading gracefully to plain chat).
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
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
from app.services import refinement as refinement_svc

log = structlog.get_logger(__name__)

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
    settings = get_settings()

    turn: AsyncIterator[Any]
    if conversation.goal is not None:
        turn = svc.run_tutor_turn(
            session,
            llm,
            learner_id=learner.id,
            conversation_id=conversation_id,
            history=history,
            user_content=data.content,
            max_tokens=settings.chat_max_tokens,
            goal=conversation.goal,
        )
    elif await refinement_svc.is_awaiting_reply(llm, conversation_id):
        turn = refinement_svc.run_refinement_turn(
            session,
            llm,
            learner_id=learner.id,
            conversation=conversation,
            user_content=data.content,
            satisfied=data.satisfied,
            max_tokens=settings.chat_max_tokens,
            max_rounds=settings.refinement_max_rounds,
            resume=True,
        )
    elif not history:
        turn = refinement_svc.run_refinement_turn(
            session,
            llm,
            learner_id=learner.id,
            conversation=conversation,
            user_content=data.content,
            satisfied=data.satisfied,
            max_tokens=settings.chat_max_tokens,
            max_rounds=settings.refinement_max_rounds,
            resume=False,
        )
    else:
        # Goal never committed, gate not mid-flight, but the conversation already has
        # history — the gate's in-memory checkpoint was lost (e.g. a restart) or this
        # conversation predates the gate. Degrade to plain chat rather than re-asking
        # "what do you want to learn?" mid-conversation.
        log.warning("refinement.gate_state_lost", conversation_id=str(conversation_id))
        turn = svc.run_tutor_turn(
            session,
            llm,
            learner_id=learner.id,
            conversation_id=conversation_id,
            history=history,
            user_content=data.content,
            max_tokens=settings.chat_max_tokens,
        )

    async def event_stream() -> AsyncIterator[str]:
        async for ev in turn:
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
            elif ev.type == "awaiting_reply":
                yield _sse({"type": "awaiting_reply", "text": ev.text, "detail": ev.detail})
            elif ev.type == "committed":
                yield _sse({"type": "committed", "goal": ev.text, "detail": ev.detail})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
