"""The interactive onboarding gate: orchestration for goal refinement + curriculum generation.

Mirrors ``app/services/refinement.py``'s shape but discards transcript after commit —
no Conversation created, in-memory checkpointer holds state for the negotiation's lifetime.
"""

import uuid
from collections.abc import AsyncIterator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.refinement import RefinementState, build_refinement_graph, refinement_config
from app.learning.curriculum import CurriculumProposal, generate_curriculum
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.rag import retrieval
from app.services import onboarding_sessions
from app.services.llm_log import log_llm_call
from app.services.turn_common import TurnEvent

log = structlog.get_logger(__name__)

ONBOARDING_SYSTEM_PROMPT = (
    "You are Guru, helping a learner turn a rough idea into a clear, scoped learning goal "
    "before a lesson begins. Read what they've said, including any feedback so far, then "
    "propose a single refined, specific version of their goal in a sentence or two, and ask "
    "whether it's right or what they'd change. Keep it short and conversational."
)


async def run_goal_refinement_turn(
    llm: LLMClient,
    session_id: str,
    user_content: str,
    satisfied: bool,
    resume: bool,
    learner_id: uuid.UUID,
) -> AsyncIterator[TurnEvent]:
    """Start or resume the goal-refinement gate, stream the proposal, then return agreed goal.

    Args:
        llm: LLM client
        session_id: A server-issued id, owned by ``learner_id`` (see
            ``app.services.onboarding_sessions``). It is namespaced by learner before it
            reaches the checkpointer, so it cannot address anyone else's negotiation.
        user_content: User's initial goal or feedback on a proposal
        satisfied: True if user has accepted the proposal
        resume: True to resume from existing state; False to start fresh
        learner_id: Whose negotiation this is. Keys the checkpoint thread, and is who the
            gate's model calls are billed to — it is a real, repeated call, up to
            `max_rounds` of them, and discarding the transcript was never a reason to
            discard the cost.

    Yields:
        TurnEvent (token, awaiting_reply, committed, error)
    """
    from langgraph.types import Command

    graph = build_refinement_graph(llm)
    config = refinement_config(onboarding_sessions.thread_key(session_id, learner_id))

    run_input: RefinementState | Command
    if resume:
        # A thread can still genuinely be gone — this learner may be presenting an id that
        # was never theirs, or one whose negotiation was already committed — and a restart is
        # no longer one of the ways (S17). Either way the graph would fail deep inside with a
        # bare KeyError; say what happened instead.
        if not (await graph.aget_state(config)).values:
            yield TurnEvent(type="error", detail="this goal session has expired; start a new one")
            return
        run_input = Command(resume={"satisfied": satisfied, "feedback": user_content})
    else:
        run_input = {
            "messages": [ChatMessage(role=ChatRole.USER, content=user_content)],
            "system": ONBOARDING_SYSTEM_PROMPT,
            "max_tokens": 500,
            "max_rounds": 5,
            "proposal": "",
            "usage": Usage(),
            "rounds": 0,
            "satisfied": False,
            "auto_committed": False,
            "agreed_goal": "",
        }

    proposal = ""
    try:
        async for mode, payload in graph.astream(
            run_input, config, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield TurnEvent(type="token", text=payload["token"])  # ty: ignore[invalid-argument-type]
            elif mode == "values":
                proposal = payload["proposal"]  # ty: ignore[invalid-argument-type]
    except Exception as exc:
        log.error("onboarding.refinement_failed", error=str(exc))
        yield TurnEvent(type="error", detail="generation failed")
        return

    snapshot = await graph.aget_state(config)
    usage = snapshot.values["usage"]
    if usage.total_tokens:
        await log_llm_call(
            learner_id=learner_id,
            role=ModelRole.FAST.value,
            spec=llm.spec(ModelRole.FAST),
            usage=usage,
        )

    if snapshot.next:
        # `propose` ran this call and paused awaiting the learner's reply — a new proposal to
        # yield.
        round_no = snapshot.values["rounds"] + 1
        yield TurnEvent(type="awaiting_reply", text=proposal, detail=f"round {round_no}")
        return

    agreed_goal = snapshot.values["agreed_goal"]
    auto_committed = snapshot.values["auto_committed"]
    yield TurnEvent(
        type="committed", text=agreed_goal, detail="auto" if auto_committed else "accepted"
    )


async def generate_curriculum_for_onboarding(
    session: AsyncSession,
    llm: LLMClient,
    goal: str,
    source_ids: list[uuid.UUID] | None,
    learner_id: uuid.UUID,
) -> CurriculumProposal | None:
    """Fetch excerpts from selected sources and generate curriculum.

    Args:
        session: Database session
        llm: LLM client
        goal: Refined goal string
        source_ids: List of source UUIDs to ground curriculum in, or None
        learner_id: The learner for scoping retrieval

    Returns:
        CurriculumProposal or None if generation fails. The call is recorded either way —
        a generation that produced unparseable output still cost what it cost.
    """
    materials = None
    if source_ids:
        # Fetch excerpts from selected sources to ground curriculum
        excerpts = []
        for source_id in source_ids:
            hits = await retrieval.retrieve(
                session,
                llm,
                goal,
                learner_id=learner_id,
                source_id=source_id,
                limit=3,
            )
            for hit in hits:
                excerpts.append(hit.text)
        if excerpts:
            materials = excerpts[:10]  # Cap total excerpts

    proposal, usage = await generate_curriculum(llm, goal, materials)
    if usage.total_tokens:
        await log_llm_call(
            learner_id=learner_id,
            role=ModelRole.SMART.value,
            spec=llm.spec(ModelRole.SMART),
            usage=usage,
        )
    return proposal
