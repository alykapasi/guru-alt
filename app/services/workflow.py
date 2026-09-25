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

from app.agent import checkpointing
from app.agent.workflow import WorkflowState, build_workflow_graph, workflow_config
from app.core.config import get_settings
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.models.assessment import Item
from app.models.chat import Conversation, Message
from app.models.knowledge import KC
from app.rag.retrieval import RetrievalHit, retrieve
from app.schemas.chat import CheckResultRead
from app.services import assessment as assessment_svc
from app.services import checkpoints, learner_context
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


async def paused_item_id(
    llm: LLMClient, session: AsyncSession, conversation_id: uuid.UUID, *, learner_id: uuid.UUID
) -> uuid.UUID | None:
    """The item a paused workflow is waiting on, or ``None`` when there is nothing to resume.

    ``aget_state`` never executes node bodies, so the real ``session``/``learner_id`` closed
    into ``build_workflow_graph`` here cost nothing extra.

    ``None`` covers two different things on purpose: no paused workflow at all, and one whose
    question has stopped being worth asking. The second is what S17's durability made
    necessary. A volatile checkpoint could not outlive much, so a paused question was never
    very stale; a durable one outlives the plan revision that changed what the learner should
    be doing and the mastery they picked up somewhere else. Resuming then would put a question
    in front of them that the system itself no longer thinks they should be answering — and
    grade the answer.

    A checkpoint that fails the check is discarded rather than left to be re-evaluated on every
    subsequent turn, and the caller sees "not paused": the turn goes to ordinary chat, which is
    the same degradation the gate already uses for state that is genuinely gone.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.learner_id != learner_id:
        return None
    graph = build_workflow_graph(
        llm, session, learner_id=learner_id, subject_id=conversation.subject_id
    )
    config = workflow_config(str(conversation_id))
    snapshot = await graph.aget_state(config)
    if not snapshot.next:
        return None
    if await checkpoints.paused_practice_is_current(
        session,
        learner_id=learner_id,
        item_id=snapshot.values.get("item_id"),
        subject_id=conversation.subject_id,
    ):
        return uuid.UUID(snapshot.values["item_id"])
    log.info("workflow.paused_state_stale", conversation_id=str(conversation_id))
    await checkpointing.discard_thread(str(conversation_id))
    return None


async def is_awaiting_reply(
    llm: LLMClient, session: AsyncSession, conversation_id: uuid.UUID, *, learner_id: uuid.UUID
) -> bool:
    """Whether the workflow is paused mid-practice *and* the question is still worth asking.

    Delegates to :func:`paused_item_id` — see there for what "worth asking" means and why a
    stale checkpoint is discarded rather than re-evaluated.
    """
    return await paused_item_id(llm, session, conversation_id, learner_id=learner_id) is not None


async def paused_prompt(
    llm: LLMClient, session: AsyncSession, conversation_id: uuid.UUID, *, learner_id: uuid.UUID
) -> tuple[str, Item] | None:
    """The paused checkpoint's last presented text and the item it belongs to, or ``None``.

    ``None`` under the same two conditions as :func:`paused_item_id` — no paused workflow, or
    one whose question has stopped being current (and is discarded there, same as there). This
    is what a resume hands back to the learner: the exact question they left, not a freshly
    rebuilt one.
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.learner_id != learner_id:
        return None
    item_id = await paused_item_id(llm, session, conversation_id, learner_id=learner_id)
    if item_id is None:
        return None
    item = await assessment_svc.get_item_for(
        session, item_id, learner_id=learner_id, subject_id=conversation.subject_id
    )
    if item is None:
        return None
    graph = build_workflow_graph(
        llm, session, learner_id=learner_id, subject_id=conversation.subject_id
    )
    snapshot = await graph.aget_state(workflow_config(str(conversation_id)))
    return snapshot.values["last_message"], item


async def run_workflow_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    user_content: str,
    rating: int | None = None,
    max_tokens: int,
    max_rounds: int,
    resume: bool,
    source_ids: Sequence[uuid.UUID] = (),
    persist_user: bool = True,
    attempt_id: uuid.UUID | None = None,
) -> AsyncIterator[TurnEvent]:
    """Start or resume the guided-practice workflow, stream it, then persist the outcome.

    ``source_ids`` is taken as an explicit param rather than read off ``conversation`` — the
    latter is an async-unsafe lazy relationship access unless the caller happened to eager-load
    it (see ``app.services.chat.get_conversation``'s ``populate_existing`` note); callers should
    resolve it once, the same way they already resolve ``conversation.subject_id``.

    ``rating`` is the learner's flashcard self-rating for this round (1-4), when the client sent
    one. It rides the resume alongside their message rather than being parsed out of it: a
    rating is a choice among four, not a sentence. A fresh start has nothing paused to rate.

    ``persist_user`` is False when the caller has already written the learner's message
    and linked it to a durable turn record (S51); the content is still carried into this
    turn's model context, it is simply not appended to the transcript a second time.

    ``attempt_id`` (S34) is this turn's idempotency key for the answer ``grade`` records, if
    any — see ``turn_svc.attempt_id_for_turn``. It rides the resume payload rather than state
    built on a fresh start, since only a resumed round can grade anything.

    The system prompt is assembled once, on the fresh start, through
    ``app.services.learner_context`` (S16) — so guided practice now carries the conversation's
    goal and what is remembered about the learner, not only the step it is practising. A resumed
    round reuses the prompt held in the checkpoint rather than rebuilding it, which is also why
    the shared context is read exactly once per practice session rather than once per round.
    """
    if persist_user:
        await add_message(session, conversation.id, ChatRole.USER.value, user_content)
        await session.commit()

    graph = build_workflow_graph(
        llm, session, learner_id=learner_id, subject_id=conversation.subject_id
    )
    config = workflow_config(str(conversation.id))
    run_input: WorkflowState | Command
    # Retrieval-grounding only happens on the fresh (not resumed) start, when `present` builds
    # the worked example — `respond`'s feedback (every resumed round) isn't source-grounded, so
    # it gets no citations. See extract_citations below, gated on `resume`.
    hits: list[RetrievalHit] = []

    if resume:
        run_input = Command(
            resume={
                "response_text": user_content,
                "rating": rating,
                # Help given while this same question was paused for a side discussion (S52) —
                # carried into the graph as WorkflowState.scaffolds, which `grade` adds to
                # `rounds`. Not reset here: it keeps counting toward every attempt on this
                # question until something that ends the question (skip, or a fresh start)
                # clears it.
                "scaffolds": conversation.practice_scaffolds,
                # This turn's answer id (S34): a retried turn resumes with the same one, so the
                # grade replays instead of recording the answer twice.
                "attempt_id": str(attempt_id) if attempt_id is not None else None,
            }
        )
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
        # A fresh start carries no help from whatever came before it. Pause/resume/skip already
        # reset this at their own moments, but a start reached without going through any of them
        # — this conversation never paused at all — still must not inherit a stale count left
        # over from an earlier question (S52, review focus 4).
        conversation.practice_scaffolds = 0

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
    item = await assessment_svc.get_item_for(
        session,
        uuid.UUID(snapshot.values["item_id"]),
        learner_id=learner_id,
        subject_id=conversation.subject_id,
    )
    item_read = item_to_read(item) if item is not None else None

    # The report for the attempt this turn graded, if it graded one. Absent on the opening
    # turn, which presents a question and has nothing to report yet.
    graded = snapshot.values.get("check_result")
    check_result = CheckResultRead.model_validate(graded) if graded else None

    citations = extract_citations(last_message, hits) if not resume else []
    # A round that generated nothing has nothing to add to the transcript. Both ``last_message``
    # and ``check_result`` ride the checkpoint, so a flashcard handed back to be rated would
    # otherwise write a fresh assistant message repeating the last one word for word, with the
    # *previous* round's report attached to it — and ``add_message`` never dedupes, so every
    # re-ask would add another. That round always ends paused at ``await_response``, which is
    # why the row can be skipped at all: ``done`` below is its only reader.
    assistant: Message | None = None
    if not snapshot.values.get("awaiting_rating"):
        assistant = await add_message(
            session,
            conversation.id,
            ChatRole.ASSISTANT.value,
            last_message,
            model=spec.model,
            citations=citations,
            check_result=check_result,
        )
    # A round can end without calling a model at all: a flashcard answered in prose is sent
    # straight back to be rated, presenting nothing new. Accounting records calls, so a round
    # that made none writes no row (same guard as ``assessment._grade``'s short-circuit).
    cost = (
        await log_llm_call(
            learner_id=learner_id,
            conversation_id=conversation.id,
            role=ModelRole.SMART.value,
            spec=spec,
            usage=usage,
        )
        if usage.total_tokens
        else None
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
        message_id=str(assistant.id) if assistant is not None else None,
        usage=usage,
        cost_usd=cost,
        item=item_read,
        detail=detail,
        citations=citations,
        check_result=check_result,
    )
