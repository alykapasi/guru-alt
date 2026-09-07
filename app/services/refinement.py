"""The interactive prompt-refinement gate: persistence orchestration around the gate graph.

Mirrors ``app/services/chat.py``'s ``run_tutor_turn`` shape (persist user turn -> stream ->
persist assistant turn) but layered over a checkpointed, resumable graph instead of a
stateless one — see ``app/agent/refinement.py`` for the HITL mechanics and the checkpointer's
known limitations.
"""

import uuid
from collections.abc import AsyncIterator

import structlog
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.refinement import RefinementState, build_refinement_graph, refinement_config
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.models.chat import Conversation
from app.services.llm_log import log_llm_call
from app.services.turn_common import TurnEvent, add_message

log = structlog.get_logger(__name__)

REFINEMENT_SYSTEM_PROMPT = (
    "You are Guru, helping a learner turn a rough idea into a clear, scoped learning goal "
    "before a lesson begins. Read what they've said, including any feedback so far, then "
    "propose a single refined, specific version of their goal in a sentence or two, and ask "
    "whether it's right or what they'd change. Keep it short and conversational."
)


async def is_awaiting_reply(llm: LLMClient, conversation_id: uuid.UUID) -> bool:
    """Whether the gate is paused mid-negotiation for this conversation.

    Only reflects state held by the process-local checkpointer — see the limitation noted in
    ``app/agent/refinement.py``.
    """
    graph = build_refinement_graph(llm)
    snapshot = await graph.aget_state(refinement_config(str(conversation_id)))
    return bool(snapshot.next)


async def run_refinement_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    user_content: str,
    satisfied: bool,
    max_tokens: int,
    max_rounds: int,
    resume: bool,
) -> AsyncIterator[TurnEvent]:
    """Start or resume the gate, stream the proposal, then persist the outcome."""
    await add_message(session, conversation.id, ChatRole.USER.value, user_content)
    await session.commit()

    graph = build_refinement_graph(llm)
    config = refinement_config(str(conversation.id))
    run_input: RefinementState | Command
    if resume:
        run_input = Command(resume={"satisfied": satisfied, "feedback": user_content})
    else:
        run_input = {
            "messages": [ChatMessage(role=ChatRole.USER, content=user_content)],
            "system": REFINEMENT_SYSTEM_PROMPT,
            "max_tokens": max_tokens,
            "max_rounds": max_rounds,
            "proposal": "",
            "usage": Usage(),
            "rounds": 0,
            "satisfied": False,
            "auto_committed": False,
            "agreed_goal": "",
        }

    spec = llm.spec(ModelRole.FAST)
    proposal = ""
    usage = Usage()
    try:
        async for mode, payload in graph.astream(
            run_input, config, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield TurnEvent(type="token", text=payload["token"])  # ty: ignore[invalid-argument-type]
            elif mode == "values":
                proposal = payload["proposal"]  # ty: ignore[invalid-argument-type]
                usage = payload["usage"]  # ty: ignore[invalid-argument-type]
    except Exception as exc:
        log.error("refinement.stream_failed", error=str(exc), model=spec.model)
        yield TurnEvent(type="error", detail="generation failed")
        return

    snapshot = await graph.aget_state(config)
    if snapshot.next:
        # `propose` ran this call and paused again awaiting the learner's reply — a genuinely
        # new proposal to persist and log. (On a resume that goes straight to `commit`,
        # `propose` never re-runs, so there's nothing new here — see the `else` branch.)
        await add_message(
            session, conversation.id, ChatRole.ASSISTANT.value, proposal, model=spec.model
        )
        await log_llm_call(
            learner_id=learner_id,
            conversation_id=conversation.id,
            role=ModelRole.FAST.value,
            spec=spec,
            usage=usage,
        )
        await session.commit()
        round_no = snapshot.values["rounds"] + 1
        yield TurnEvent(type="awaiting_reply", text=proposal, detail=f"round {round_no}")
        return

    agreed_goal = snapshot.values["agreed_goal"]
    auto_committed = snapshot.values["auto_committed"]
    conversation.goal = agreed_goal
    await session.commit()
    yield TurnEvent(
        type="committed", text=agreed_goal, detail="auto" if auto_committed else "accepted"
    )
