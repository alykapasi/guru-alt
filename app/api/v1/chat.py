"""Chat endpoints: conversations + an SSE streaming tutor turn.

``send_message`` dispatches each turn to one of four LangGraph flows: a one-off agentic
tool-using action (``mode="agentic"``, checked first — it bypasses goal negotiation
entirely), the guided-practice workflow (``mode="workflow"``, or whenever one is already
paused mid-practice for this conversation — checked next, also bypassing goal negotiation),
the interactive refinement gate (while the conversation has no committed ``goal`` yet), or
the plain tutor turn (once a goal is committed, or the gate was never entered). See
``app/services/refinement.py`` for the gate's persistence orchestration and its dispatch edge
cases (e.g. a lost in-memory checkpoint degrading gracefully to plain chat),
``app/services/agentic.py`` for the agentic turn, and ``app/services/workflow.py`` for the
workflow turn.
"""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.core.config import get_settings
from app.llm import LLMClient
from app.models.chat import Conversation, ConversationPhase
from app.models.source import Source
from app.schemas.chat import (
    ChatTurnRequest,
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
)
from app.services import agentic as agentic_svc
from app.services import budget, turn_lock
from app.services import chat as svc
from app.services import knowledge as knowledge_svc
from app.services import refinement as refinement_svc
from app.services import workflow as workflow_svc

log = structlog.get_logger(__name__)

router = APIRouter(tags=["chat"])


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    data: ConversationCreate, session: SessionDep, learner: CurrentLearner
):
    if data.subject_id is not None:
        if await knowledge_svc.get_subject(session, data.subject_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "subject not found")
    if data.kind == "session" and data.subject_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "a session conversation requires a subject_id"
        )
    if data.source_ids:
        if data.subject_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "source_ids requires a subject_id to narrow within"
            )
        sources = (
            await session.scalars(select(Source).where(Source.id.in_(data.source_ids)))
        ).all()
        if len(sources) != len(set(data.source_ids)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "one or more sources not found")
        for source in sources:
            if source.learner_id != learner.id or source.subject_id != data.subject_id:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "one or more sources don't belong to this learner/subject",
                )
    return await svc.create_conversation(
        session,
        learner.id,
        data.title,
        kind=data.kind,
        subject_id=data.subject_id,
        source_ids=data.source_ids,
    )


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(session: SessionDep, learner: CurrentLearner):
    return await svc.list_conversations(session, learner.id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
async def update_conversation(
    conversation_id: uuid.UUID,
    data: ConversationUpdate,
    session: SessionDep,
    learner: CurrentLearner,
):
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return await svc.update_conversation_title(session, conversation, data.title)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner
):
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    await svc.delete_conversation(session, conversation)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return await svc.list_messages(session, conversation_id)


class TurnFlow(StrEnum):
    """Which of the four flows a turn was routed to."""

    REFINEMENT = "refinement"
    TUTOR = "tutor"
    AGENTIC = "agentic"
    WORKFLOW = "workflow"


@dataclass(frozen=True)
class DispatchedTurn:
    """The chosen flow, its not-yet-started stream, and the state the choice was made against."""

    flow: TurnFlow
    stream: AsyncIterator[Any]
    workflow_paused: bool


def _phase_after(
    flow: TurnFlow, *, awaiting_reply: bool, workflow_paused: bool
) -> ConversationPhase:
    """What the conversation is waiting for now the turn has ended.

    Two signals, and both are needed. ``awaiting_reply`` says the turn ended by asking the
    learner for something rather than answering them — but the gate and the workflow both emit
    it, for entirely different things, so the flow says *what* is being asked for. A tutor or
    agentic reply asks for nothing, which is exactly the case the frontend's old inference got
    wrong: it read "no goal, assistant spoke last" as a goal proposal.

    A committing gate turn emits ``committed`` and never ``awaiting_reply`` (see
    ``app/services/refinement.py``), so commitment needs no separate flag here.

    ``workflow_paused`` covers the one case where this turn's own events are not the whole
    story: ``mode="agentic"`` is checked before a paused workflow, so an agentic interjection
    steps *around* a practice item without answering it. The item is still in play afterwards,
    and the next message resumes it — so reporting CHATTING would be a phase that disagrees
    with what the very next turn does.
    """
    if awaiting_reply:
        if flow is TurnFlow.WORKFLOW:
            return ConversationPhase.AWAITING_ANSWER
        if flow is TurnFlow.REFINEMENT:
            return ConversationPhase.GOAL_PROPOSED
    if workflow_paused and flow is not TurnFlow.WORKFLOW:
        return ConversationPhase.AWAITING_ANSWER
    return ConversationPhase.CHATTING


async def _dispatch_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    data: ChatTurnRequest,
) -> DispatchedTurn:
    """Pick the flow this turn belongs to and return it with its (not yet started) stream.

    The flow is returned rather than inferred later because only here is the choice actually
    made; downstream, a refinement proposal and a tutor reply are both "an assistant message".
    """
    conversation_id = conversation.id
    settings = get_settings()
    # The window a turn carries, not the whole transcript — see svc.recent_messages. The gate
    # branch below asks whether the conversation is *empty*, which this still answers: a
    # truncated window is never empty.
    history = await svc.recent_messages(
        session, conversation_id, limit=settings.chat_history_max_messages
    )
    workflow_awaiting = await workflow_svc.is_awaiting_reply(
        llm, session, conversation_id, learner_id=learner_id
    )

    if data.mode == "agentic":
        return DispatchedTurn(
            TurnFlow.AGENTIC,
            workflow_paused=workflow_awaiting,
            stream=agentic_svc.run_agentic_turn(
                session,
                llm,
                learner_id=learner_id,
                conversation_id=conversation_id,
                history=history,
                user_content=data.content,
                max_tokens=settings.chat_max_tokens,
                subject_id=conversation.subject_id,
                source_ids=conversation.source_ids,
            ),
        )
    if data.mode == "workflow" or workflow_awaiting:
        return DispatchedTurn(
            TurnFlow.WORKFLOW,
            workflow_paused=workflow_awaiting,
            stream=workflow_svc.run_workflow_turn(
                session,
                llm,
                learner_id=learner_id,
                conversation=conversation,
                user_content=data.content,
                max_tokens=settings.chat_max_tokens,
                max_rounds=settings.workflow_max_rounds,
                resume=workflow_awaiting,
                source_ids=conversation.source_ids,
            ),
        )
    if conversation.goal is not None:
        return DispatchedTurn(
            TurnFlow.TUTOR,
            workflow_paused=workflow_awaiting,
            stream=svc.run_tutor_turn(
                session,
                llm,
                learner_id=learner_id,
                conversation_id=conversation_id,
                history=history,
                user_content=data.content,
                max_tokens=settings.chat_max_tokens,
                goal=conversation.goal,
                subject_id=conversation.subject_id,
                source_ids=conversation.source_ids,
            ),
        )
    if await refinement_svc.is_awaiting_reply(llm, conversation_id):
        return DispatchedTurn(
            TurnFlow.REFINEMENT,
            workflow_paused=workflow_awaiting,
            stream=refinement_svc.run_refinement_turn(
                session,
                llm,
                learner_id=learner_id,
                conversation=conversation,
                user_content=data.content,
                satisfied=data.satisfied,
                max_tokens=settings.chat_max_tokens,
                max_rounds=settings.refinement_max_rounds,
                resume=True,
            ),
        )
    if not history:
        return DispatchedTurn(
            TurnFlow.REFINEMENT,
            workflow_paused=workflow_awaiting,
            stream=refinement_svc.run_refinement_turn(
                session,
                llm,
                learner_id=learner_id,
                conversation=conversation,
                user_content=data.content,
                satisfied=data.satisfied,
                max_tokens=settings.chat_max_tokens,
                max_rounds=settings.refinement_max_rounds,
                resume=False,
            ),
        )
    # Goal never committed, gate not mid-flight, but the conversation already has history —
    # the gate's in-memory checkpoint was lost (e.g. a restart) or this conversation predates
    # the gate. Degrade to plain chat rather than re-asking "what do you want to learn?"
    # mid-conversation.
    log.warning("refinement.gate_state_lost", conversation_id=str(conversation_id))
    return DispatchedTurn(
        TurnFlow.TUTOR,
        workflow_paused=workflow_awaiting,
        stream=svc.run_tutor_turn(
            session,
            llm,
            learner_id=learner_id,
            conversation_id=conversation_id,
            history=history,
            user_content=data.content,
            max_tokens=settings.chat_max_tokens,
            subject_id=conversation.subject_id,
            source_ids=conversation.source_ids,
        ),
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    data: ChatTurnRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
) -> StreamingResponse:
    """Persist the user turn, then stream the tutor's reply as Server-Sent Events.

    One turn at a time per conversation: an overlapping request is refused rather than
    allowed to interleave messages or resume the same paused graph twice (see
    ``app.services.turn_lock``). The claim is held until the stream ends, however it ends.
    """
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    try:
        await budget.require_budget(session, learner.id, get_settings())
    except budget.BudgetExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    if not turn_lock.claim(conversation_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a turn is already in progress for this conversation"
        )
    try:
        dispatched = await _dispatch_turn(
            session, llm, learner_id=learner.id, conversation=conversation, data=data
        )
    except Exception:
        turn_lock.release(conversation_id)
        raise

    async def event_stream() -> AsyncIterator[str]:
        awaiting_reply = False
        active_item: uuid.UUID | None = None
        async for ev in dispatched.stream:
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
                        "item": ev.item.model_dump(mode="json") if ev.item else None,
                        "detail": ev.detail,
                        "citations": ev.citations,
                    }
                )
            elif ev.type == "awaiting_reply":
                awaiting_reply = True
                active_item = ev.item.id if ev.item else None
                yield _sse(
                    {
                        "type": "awaiting_reply",
                        "text": ev.text,
                        "detail": ev.detail,
                        "item": ev.item.model_dump(mode="json") if ev.item else None,
                        "citations": ev.citations,
                    }
                )
            elif ev.type == "committed":
                yield _sse({"type": "committed", "goal": ev.text, "detail": ev.detail})
            elif ev.type == "tool_call":
                yield _sse({"type": "tool_call", "detail": ev.detail})

        # Record the phase only after the stream drains, so an interrupted turn leaves the
        # previous phase standing rather than a half-decided one.
        await svc.record_phase(
            session,
            conversation_id,
            _phase_after(
                dispatched.flow,
                awaiting_reply=awaiting_reply,
                workflow_paused=dispatched.workflow_paused,
            ),
            active_item_id=active_item,
        )

    async def guarded_stream() -> AsyncIterator[str]:
        try:
            async for chunk in event_stream():
                yield chunk
        finally:
            turn_lock.release(conversation_id)

    return StreamingResponse(guarded_stream(), media_type="text/event-stream")
