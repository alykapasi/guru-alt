"""LangGraph refinement-gate substrate: propose/interrupt/resume/commit via FakeProvider."""

from langgraph.types import Command

from app.agent.refinement import RefinementState, build_refinement_graph, refinement_config
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, Usage

REPLY = "Sounds like light reactions."


def _state(goal: str = "I want to learn photosynthesis") -> RefinementState:
    return {
        "messages": [ChatMessage(role=ChatRole.USER, content=goal)],
        "system": "You are a goal-refining tutor.",
        "max_tokens": 256,
        "max_rounds": 3,
        "proposal": "",
        "usage": Usage(),
        "rounds": 0,
        "satisfied": False,
        "auto_committed": False,
        "agreed_goal": "",
    }


async def test_propose_streams_tokens_and_fills_proposal() -> None:
    graph = build_refinement_graph(fake_llm_client(REPLY))
    config = refinement_config("t-propose")
    tokens: list[str] = []
    async for mode, payload in graph.astream(_state(), config, stream_mode=["custom", "values"]):
        if mode == "custom":
            tokens.append(payload["token"])  # ty: ignore[invalid-argument-type]
    assert "".join(tokens) == REPLY
    snapshot = await graph.aget_state(config)
    assert snapshot.values["proposal"] == REPLY


async def test_first_invoke_pauses_at_ask_learner() -> None:
    graph = build_refinement_graph(fake_llm_client(REPLY))
    config = refinement_config("t-pause")
    await graph.ainvoke(_state(), config)
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("ask_learner",)
    assert snapshot.interrupts[0].value == {"proposal": REPLY, "round": 1}


async def test_resume_satisfied_commits() -> None:
    graph = build_refinement_graph(fake_llm_client(REPLY))
    config = refinement_config("t-satisfied")
    await graph.ainvoke(_state(), config)
    result = await graph.ainvoke(
        Command(resume={"satisfied": True, "feedback": "yes exactly"}), config
    )
    assert result["agreed_goal"] == REPLY
    assert result["auto_committed"] is False
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()


async def test_resume_unsatisfied_loops_back() -> None:
    graph = build_refinement_graph(fake_llm_client(REPLY))
    config = refinement_config("t-loop")
    await graph.ainvoke(_state(), config)
    await graph.ainvoke(
        Command(resume={"satisfied": False, "feedback": "more on dark reactions"}), config
    )
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("ask_learner",)
    assert snapshot.interrupts[0].value == {"proposal": REPLY, "round": 2}
    assert snapshot.values["rounds"] == 1


async def test_max_rounds_auto_commits() -> None:
    graph = build_refinement_graph(fake_llm_client(REPLY))
    config = refinement_config("t-max-rounds")
    await graph.ainvoke(_state(), config)
    for _ in range(2):  # rounds 2, 3 (max_rounds=3)
        await graph.ainvoke(Command(resume={"satisfied": False, "feedback": "no"}), config)
    result = await graph.ainvoke(Command(resume={"satisfied": False, "feedback": "no"}), config)
    assert result["auto_committed"] is True
    assert result["agreed_goal"] == REPLY
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()
