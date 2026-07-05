"""LangGraph tutor substrate: the graph + node stream and fill state via FakeProvider."""

from app.agent.tutor import TutorState, build_tutor_graph
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, Usage

REPLY = "Let us explore this together."


def _state(user: str = "What is a derivative?") -> TutorState:
    return {
        "messages": [ChatMessage(role=ChatRole.USER, content=user)],
        "system": "You are a tutor.",
        "max_tokens": 256,
        "reply": "",
        "usage": Usage(),
    }


async def test_generate_node_fills_reply_and_usage() -> None:
    graph = build_tutor_graph(fake_llm_client(REPLY))
    final = await graph.ainvoke(_state())
    assert final["reply"] == REPLY
    assert final["usage"].output_tokens == len(REPLY.split())


async def test_graph_streams_tokens_incrementally() -> None:
    graph = build_tutor_graph(fake_llm_client(REPLY))
    tokens: list[str] = []
    async for mode, payload in graph.astream(_state(), stream_mode=["custom", "values"]):
        if mode == "custom":
            tokens.append(payload["token"])  # ty: ignore[invalid-argument-type]
    # One custom payload per streamed word chunk (proves incremental, not buffered).
    assert len(tokens) == len(REPLY.split())
    assert "".join(tokens) == REPLY
