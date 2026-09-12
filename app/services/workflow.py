"""The guided-practice workflow: persistence orchestration around the workflow graph.

Mirrors ``app/services/refinement.py``'s shape (checkpointed, resumable graph; graceful
degradation) but wraps ``app/agent/workflow.py``'s practice loop instead of the goal-negotiation
gate — see there for the interrupt/grade mechanics, and its module docstring for the
in-memory-checkpointer's known limitations (shared with the refinement gate).
"""

import uuid
from collections.abc import AsyncIterator, Sequence

import structlog
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.workflow import WorkflowState, build_workflow_graph, workflow_config
from app.core.config import get_settings
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.models.chat import Conversation
from app.models.knowledge import KC
from app.rag.retrieval import RetrievalHit, retrieve
from app.schemas.chat import CheckResultRead
from app.services import assessment as assessment_svc
from app.services import learner_context
from app.services.assessment import item_to_read
from app.services.llm_log import log_llm_call
from app.services.session_runner import short_answer_item_for_kc
from app.services.turn_common import (
    TurnEvent,
    add_message,
    extract_citations,
    format_grounding,
)

log = structlog.get_logger(__name__)

WORKFLOW_SYSTEM_PROMPT = (
    "You are Guru, running a short guided-practice walkthrough. Give a brief worked example "
    "for the knowledge component below, then pose that exact practice problem for the learner "
    "to attempt in their own words — do not invent a different problem, since their answer is "
    "graded against this one. Keep it focused and conversational."
)


async def is_awaiting_reply(
    llm: LLMClient, session: AsyncSession, conversation_id: uuid.UUID, *, learner_id: uuid.UUID
) -> bool:
    """Whether the workflow is paused mid-practice for this conversation.

    Only reflects state held by the process-local checkpointer — see the limitation noted in
    ``app/agent/workflow.py``. ``aget_state`` never executes node bodies, so the real
    ``session``/``learner_id`` closed into ``build_workflow_graph`` here cost nothing extra.
    """
    graph = build_workflow_graph(llm, session, learner_id=learner_id)
    snapshot = await graph.aget_state(workflow_config(str(conversation_id)))
    return bool(snapshot.next)


async def run_workflow_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    user_content: str,
    max_tokens: int,
    max_rounds: int,
    resume: bool,
    source_ids: Sequence[uuid.UUID] = (),
    persist_user: bool = True,
) -> AsyncIterator[TurnEvent]:
    """Start or resume the guided-practice workflow, stream it, then persist the outcome.

    ``source_ids`` is taken as an explicit param rather than read off ``conversation`` — the
    latter is an async-unsafe lazy relationship access unless the caller happened to eager-load
    it (see ``app.services.chat.get_conversation``'s ``populate_existing`` note); callers should
    resolve it once, the same way they already resolve ``conversation.subject_id``.

    ``persist_user`` is False when the caller has already written the learner's message
    and linked it to a durable turn record (S51); the content is still carried into this
    turn's model context, it is simply not appended to the transcript a second time.

    The system prompt is assembled once, on the fresh start, through
    ``app.services.learner_context`` (S16) — so guided practice now carries the conversation's
    goal and what is remembered about the learner, not only the step it is practising. A resumed
    round reuses the prompt held in the checkpoint rather than rebuilding it, which is also why
    the shared context is read exactly once per practice session rather than once per round.
    """
    if persist_user:
        await add_message(session, conversation.id, ChatRole.USER.value, user_content)
        await session.commit()

    graph = build_workflow_graph(llm, session, learner_id=learner_id)
    config = workflow_config(str(conversation.id))
    run_input: WorkflowState | Command
    # Retrieval-grounding only happens on the fresh (not resumed) start, when `present` builds
    # the worked example — `respond`'s feedback (every resumed round) isn't source-grounded, so
    # it gets no citations. See extract_citations below, gated on `resume`.
    hits: list[RetrievalHit] = []

    if resume:
        run_input = Command(resume={"response_text": user_content})
    else:
        context = await learner_context.gather(
            session, llm, learner_id=learner_id, conversation=conversation, query=user_content
        )
        step = context.plan
        if step is None:
            yield TurnEvent(type="error", detail="no active lesson-plan step to practice")
            return
        kc = await session.get(KC, step.kc_id)
        if kc is None:
            yield TurnEvent(type="error", detail="no active lesson-plan step to practice")
            return
        item = await short_answer_item_for_kc(session, llm, learner_id=learner_id, kc=kc)
        if item is None:
            yield TurnEvent(
                type="error", detail="couldn't prepare a practice item for the current step"
            )
            return
        grounding = None
        if conversation.subject_id is not None:
            hits = await retrieve(
                session,
                llm,
                step.kc_name,
                learner_id=learner_id,
                subject_id=conversation.subject_id,
                source_ids=source_ids or None,
                limit=get_settings().chat_grounding_limit,
            )
            grounding = format_grounding(hits)
        system = learner_context.compose(
            WORKFLOW_SYSTEM_PROMPT,
            context,
            extra=[f"Knowledge component: {step.kc_name}. Practice problem: {item.stem}"],
            grounding=grounding,
            # The problem is already chosen and the prompt above forbids substituting another,
            # so the item-selection hints are withheld — see ``learner_context``.
            task_fixed=True,
        )
        run_input = {
            "messages": [ChatMessage(role=ChatRole.USER, content=user_content)],
            "system": system,
            "max_tokens": max_tokens,
            "item_id": str(item.id),
            "response_text": "",
            "last_message": "",
            "score": 0.0,
            "correct": False,
            "usage": Usage(),
            "rounds": 0,
            "max_rounds": max_rounds,
        }

    spec = llm.spec(ModelRole.SMART)
    last_message = ""
    usage = Usage()
    try:
        async for mode, payload in graph.astream(
            run_input, config, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield TurnEvent(type="token", text=payload["token"])  # ty: ignore[invalid-argument-type]
            elif mode == "values":
                last_message = payload["last_message"]  # ty: ignore[invalid-argument-type]
                usage = payload["usage"]  # ty: ignore[invalid-argument-type]
    except Exception as exc:
        log.error("workflow.stream_failed", error=str(exc), model=spec.model)
        yield TurnEvent(type="error", detail="generation failed")
        return

    snapshot = await graph.aget_state(config)
    item = await assessment_svc.get_item(session, uuid.UUID(snapshot.values["item_id"]))
    item_read = item_to_read(item) if item is not None else None

    # The report for the attempt this turn graded, if it graded one. Absent on the opening
    # turn, which presents a question and has nothing to report yet.
    graded = snapshot.values.get("check_result")
    check_result = CheckResultRead.model_validate(graded) if graded else None

    citations = extract_citations(last_message, hits) if not resume else []
    assistant = await add_message(
        session,
        conversation.id,
        ChatRole.ASSISTANT.value,
        last_message,
        model=spec.model,
        citations=citations,
        check_result=check_result,
    )
    cost = await log_llm_call(
        learner_id=learner_id,
        conversation_id=conversation.id,
        role=ModelRole.SMART.value,
        spec=spec,
        usage=usage,
    )
    await session.commit()

    if snapshot.next:
        yield TurnEvent(
            type="awaiting_reply",
            text=last_message,
            detail="practice",
            item=item_read,
            citations=citations,
            # The round that matters most for this: the learner has answered, is being given
            # a hint, and is about to answer again — so what the last attempt actually did is
            # the thing they need in front of them (S15).
            check_result=check_result,
        )
        return

    detail = "mastered" if snapshot.values["correct"] else "capped"
    yield TurnEvent(
        type="done",
        message_id=str(assistant.id),
        usage=usage,
        cost_usd=cost,
        item=item_read,
        detail=detail,
        citations=citations,
        check_result=check_result,
    )
