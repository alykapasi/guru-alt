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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentLearner, EngineDep, LLMClientDep, SessionDep
from app.core.config import get_settings
from app.llm import LLMClient
from app.models.chat import Conversation, ConversationPhase, Message, TurnStatus
from app.models.source import Source
from app.schemas.chat import (
    ChatTurnRequest,
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
    TurnRead,
)
from app.services import agentic as agentic_svc
from app.services import budget, turn_lock
from app.services import chat as svc
from app.services import knowledge as knowledge_svc
from app.services import refinement as refinement_svc
from app.services import turn as turn_svc
from app.services import workflow as workflow_svc
from app.services.turn_common import TurnEvent

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
class FlowChoice:
    """Which of the four flows a turn belongs to, and the state the choice was made against."""

    flow: TurnFlow
    workflow_paused: bool
    resume: bool  # only meaningful for the workflow and refinement graphs


def _open_check_id(event: TurnEvent) -> uuid.UUID | None:
    """The item a ``done`` event leaves awaiting an answer, if any.

    Only a tutor turn's own check qualifies, and it says so by marking the item ``check``
    (S15). Every other item on a done event is informational rather than pending: the
    guided-practice workflow reports the item it has just *finished* grading, so recording
    that one as active would leave the conversation waiting for an answer to a question the
    learner already answered — and hand their next message to the grader for it.
    """
    if event.detail != "check" or event.item is None:
        return None
    return event.item.id


def _phase_after(
    flow: TurnFlow, *, awaiting_reply: bool, workflow_paused: bool, check_open: bool
) -> ConversationPhase:
    """What the conversation is waiting for now the turn has ended.

    Three signals, and all three are needed. ``awaiting_reply`` says the turn ended by asking
    the learner for something rather than answering them — but the gate and the workflow both
    emit it, for entirely different things, so the flow says *what* is being asked for. A tutor
    or agentic reply asks for nothing, which is exactly the case the frontend's old inference
    got wrong: it read "no goal, assistant spoke last" as a goal proposal.

    ``check_open`` is the tutor flow's version of the same claim (S15): a plain-chat turn that
    left a practice question with the learner *is* waiting on an answer, and saying so is what
    makes the next message reach the grader instead of being read as more conversation.

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
    if check_open:
        return ConversationPhase.AWAITING_ANSWER
    return ConversationPhase.CHATTING


async def _choose_flow(
    llm: LLMClient,
    *,
    conversation: Conversation,
    data: ChatTurnRequest,
    history: Sequence[Message],
    learner_id: uuid.UUID,
    session: AsyncSession,
) -> FlowChoice:
    """Pick the flow this turn belongs to, before anything is generated or persisted.

    Separate from building the stream because the turn record (S51) has to be opened — and
    committed — between the two: the flow is part of what an interrupted turn should say about
    itself, and opening the turn is what writes the learner's message.
    """
    workflow_awaiting = await workflow_svc.is_awaiting_reply(
        llm, session, conversation.id, learner_id=learner_id
    )
    if data.mode == "agentic":
        return FlowChoice(TurnFlow.AGENTIC, workflow_paused=workflow_awaiting, resume=False)
    if data.mode == "workflow" or workflow_awaiting:
        return FlowChoice(
            TurnFlow.WORKFLOW, workflow_paused=workflow_awaiting, resume=workflow_awaiting
        )
    if conversation.goal is not None:
        return FlowChoice(TurnFlow.TUTOR, workflow_paused=workflow_awaiting, resume=False)
    if await refinement_svc.is_awaiting_reply(llm, conversation.id):
        return FlowChoice(TurnFlow.REFINEMENT, workflow_paused=workflow_awaiting, resume=True)
    if not history:
        return FlowChoice(TurnFlow.REFINEMENT, workflow_paused=workflow_awaiting, resume=False)
    # Goal never committed, gate not mid-flight, but the conversation already has history —
    # the gate's in-memory checkpoint was lost (e.g. a restart) or this conversation predates
    # the gate. Degrade to plain chat rather than re-asking "what do you want to learn?"
    # mid-conversation.
    log.warning("refinement.gate_state_lost", conversation_id=str(conversation.id))
    return FlowChoice(TurnFlow.TUTOR, workflow_paused=workflow_awaiting, resume=False)


def _build_stream(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    data: ChatTurnRequest,
    choice: FlowChoice,
    history: Sequence[Message],
) -> AsyncIterator[Any]:
    """The chosen flow's (not yet started) stream.

    Every flow is told ``persist_user=False``: the learner's message was already written when
    the turn was opened, so writing it again here would duplicate it on a retry.
    """
    settings = get_settings()
    if choice.flow is TurnFlow.AGENTIC:
        return agentic_svc.run_agentic_turn(
            session,
            llm,
            learner_id=learner_id,
            conversation=conversation,
            history=history,
            user_content=data.content,
            max_tokens=settings.chat_max_tokens,
            source_ids=conversation.source_ids,
            persist_user=False,
        )
    if choice.flow is TurnFlow.WORKFLOW:
        return workflow_svc.run_workflow_turn(
            session,
            llm,
            learner_id=learner_id,
            conversation=conversation,
            user_content=data.content,
            max_tokens=settings.chat_max_tokens,
            max_rounds=settings.workflow_max_rounds,
            resume=choice.resume,
            source_ids=conversation.source_ids,
            persist_user=False,
        )
    if choice.flow is TurnFlow.REFINEMENT:
        return refinement_svc.run_refinement_turn(
            session,
            llm,
            learner_id=learner_id,
            conversation=conversation,
            user_content=data.content,
            satisfied=data.satisfied,
            max_tokens=settings.chat_max_tokens,
            max_rounds=settings.refinement_max_rounds,
            resume=choice.resume,
            persist_user=False,
        )
    return svc.run_tutor_turn(
        session,
        llm,
        learner_id=learner_id,
        conversation=conversation,
        history=history,
        user_content=data.content,
        max_tokens=settings.chat_max_tokens,
        source_ids=conversation.source_ids,
        persist_user=False,
    )


@router.get("/conversations/{conversation_id}/turns", response_model=list[TurnRead])
async def list_turns(
    conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner, limit: int = 5
):
    """The conversation's most recent turns, newest first — how an interruption becomes visible.

    Reading this also reaps turns abandoned by a disconnect or a restart, so a stranded
    ``pending`` row is reported as ``cancelled`` rather than as work still in progress.
    """
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return await turn_svc.recent_turns(session, conversation_id, limit=max(1, min(limit, 50)))


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    data: ChatTurnRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
    db_engine: EngineDep,
) -> StreamingResponse:
    """Persist the user turn, then stream the tutor's reply as Server-Sent Events.

    One turn at a time per conversation: an overlapping request is refused rather than
    allowed to interleave messages or resume the same paused graph twice (see
    ``app.services.turn_lock``). The claim is held until the stream ends, however it ends.

    The turn is recorded before generation and closed on every path out of it (S51). A client
    that supplies ``client_turn_id`` gets retry for free: repeating a failed turn regenerates
    from the same learner message, and repeating a completed one is refused rather than
    answered twice.
    """
    conversation = await svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    try:
        await budget.require_budget(session, learner.id, get_settings())
    except budget.BudgetExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    # Before the claim, not after: reaping reads the claim as its liveness signal, so our own
    # would make this conversation's abandoned turns look alive.
    await turn_svc.reap_stale(session, conversation_id)

    claim = await turn_lock.claim(db_engine, conversation_id)
    if claim is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a turn is already in progress for this conversation"
        )
    try:
        existing = await turn_svc.resolve_client_turn(session, conversation_id, data.client_turn_id)
        settings = get_settings()
        # The window a turn carries, not the whole transcript — see svc.recent_messages. On a
        # retry it already contains this turn's own message, which ``history_without`` removes:
        # the flow re-appends the content itself, and the "is this conversation empty?" test
        # that routes a first turn to the gate has to answer the same way it did the first time.
        history = turn_svc.history_without(
            await svc.recent_messages(
                session, conversation_id, limit=settings.chat_history_max_messages
            ),
            existing,
        )
        choice = await _choose_flow(
            llm,
            conversation=conversation,
            data=data,
            history=history,
            learner_id=learner.id,
            session=session,
        )
        turn = await turn_svc.open_turn(
            session,
            conversation_id=conversation_id,
            flow=choice.flow.value,
            content=data.content,
            client_turn_id=data.client_turn_id,
            existing=existing,
        )
        stream = _build_stream(
            session,
            llm,
            learner_id=learner.id,
            conversation=conversation,
            data=data,
            choice=choice,
            history=history,
        )
    except turn_svc.TurnAlreadyCompleted as exc:
        await claim.release()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this turn has already been answered"
        ) from exc
    except Exception:
        await claim.release()
        raise

    async def event_stream() -> AsyncIterator[str]:
        awaiting_reply = False
        check_open = False
        active_item: uuid.UUID | None = None
        # None until a terminal event arrives. A stream that ends without one did not finish —
        # the client's old reading of EOF as success is exactly what this records against.
        outcome: TurnStatus | None = None
        assistant_message_id: uuid.UUID | None = None
        error: str | None = None
        async for ev in stream:
            if ev.type == "token":
                yield _sse({"type": "token", "text": ev.text})
            elif ev.type == "error":
                outcome, error = TurnStatus.FAILED, ev.detail
                yield _sse({"type": "error", "detail": ev.detail})
            elif ev.type == "done":
                outcome = TurnStatus.COMPLETED
                assistant_message_id = uuid.UUID(ev.message_id) if ev.message_id else None
                posed = _open_check_id(ev)
                if posed is not None:
                    check_open, active_item = True, posed
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
                        # Present only on a turn that graded an answer the learner gave in
                        # conversation (S15) — null on every other turn, which is most of them.
                        "check_result": (
                            ev.check_result.model_dump(mode="json") if ev.check_result else None
                        ),
                    }
                )
            elif ev.type == "awaiting_reply":
                awaiting_reply = True
                outcome = TurnStatus.COMPLETED
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
                outcome = TurnStatus.COMPLETED
                yield _sse({"type": "committed", "goal": ev.text, "detail": ev.detail})
            elif ev.type == "tool_call":
                yield _sse({"type": "tool_call", "detail": ev.detail})

        # Record the phase only after the stream drains, so an interrupted turn leaves the
        # previous phase standing rather than a half-decided one.
        await svc.record_phase(
            session,
            conversation_id,
            _phase_after(
                choice.flow,
                awaiting_reply=awaiting_reply,
                workflow_paused=choice.workflow_paused,
                check_open=check_open,
            ),
            active_item_id=active_item,
        )
        await turn_svc.close_turn(
            session,
            turn.id,
            outcome or TurnStatus.CANCELLED,
            assistant_message_id=assistant_message_id,
            error=error if outcome else "the stream ended without a terminal event",
        )

    async def guarded_stream() -> AsyncIterator[str]:
        # A client that disconnects mid-stream never reaches event_stream's closing writes, so
        # the turn stays `pending` and is reaped as `cancelled` on the next read. Recording it
        # here instead is not an option: the request task is already being cancelled, so any
        # await in this finally is cancelled with it.
        try:
            async for chunk in event_stream():
                yield chunk
        finally:
            await claim.release()

    return StreamingResponse(guarded_stream(), media_type="text/event-stream")
