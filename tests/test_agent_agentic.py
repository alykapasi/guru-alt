"""Bounded agentic LangGraph loop: call_model <-> execute_tools, scripted via FakeProvider.

Mirrors test_agent_tutor.py's graph-unit style. Uses a trivial in-file echo tool rather than
the real search_materials tool, keeping this test independent of the DB.
"""

from app.agent.agentic import AgenticState, build_agentic_graph
from app.agent.tools import Tool, ToolResult
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, ToolCall, Usage


def _state(max_iterations: int = 4) -> AgenticState:
    return {
        "messages": [ChatMessage(role=ChatRole.USER, content="search for x")],
        "system": "You are an agent.",
        "max_tokens": 256,
        "max_iterations": max_iterations,
        "iterations": 0,
        "reply": "",
        "usage": Usage(),
        "pending_tool_calls": [],
        "tool_events": [],
    }


async def _echo(args: dict) -> ToolResult:
    return ToolResult(content=f"got {args}")


_ECHO_TOOL = Tool(
    name="echo",
    description="Echoes its input.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    execute=_echo,
)


async def test_tool_call_turn_routes_through_execute_tools_and_loops_back() -> None:
    script = [
        FakeTurn(tool_calls=[ToolCall(id="t1", name="echo", input={"text": "hi"})]),
        FakeTurn(text="here is the answer"),
    ]
    graph = build_agentic_graph(fake_llm_client(script=script), [_ECHO_TOOL])

    final = await graph.ainvoke(_state())

    assert final["reply"] == "here is the answer"
    assert final["iterations"] == 1
    assert final["pending_tool_calls"] == []
    assert len(final["tool_events"]) == 1
    event = final["tool_events"][0]
    assert event.name == "echo"
    assert event.input == {"text": "hi"}
    assert "hi" in event.result
    assert not event.is_error


async def test_unknown_tool_name_is_an_error_event_not_a_crash() -> None:
    script = [
        FakeTurn(tool_calls=[ToolCall(id="t1", name="nonexistent", input={})]),
        FakeTurn(text="done"),
    ]
    graph = build_agentic_graph(fake_llm_client(script=script), [_ECHO_TOOL])

    final = await graph.ainvoke(_state())

    assert final["reply"] == "done"
    event = final["tool_events"][0]
    assert event.is_error


async def test_max_iterations_bounds_an_unbounded_tool_calling_loop() -> None:
    # Every scripted turn returns a tool call — an unbounded model would loop forever.
    always_calls = FakeTurn(tool_calls=[ToolCall(id="t1", name="echo", input={})])
    graph = build_agentic_graph(fake_llm_client(script=[always_calls] * 5), [_ECHO_TOOL])

    final = await graph.ainvoke(_state(max_iterations=1))

    assert final["iterations"] == 1  # exactly one execute_tools round, not unbounded
    assert final["pending_tool_calls"] != []  # the cap stopped it mid-loop, not naturally


async def test_streams_tokens_incrementally_during_call_model() -> None:
    graph = build_agentic_graph(
        fake_llm_client(script=[FakeTurn(text="hello world")]), [_ECHO_TOOL]
    )
    tokens: list[str] = []
    async for mode, payload in graph.astream(_state(), stream_mode=["custom", "values"]):
        if mode == "custom" and "token" in payload:
            tokens.append(payload["token"])  # ty: ignore[invalid-argument-type]
    assert "".join(tokens) == "hello world"


async def test_usage_accumulates_across_iterations() -> None:
    script = [
        FakeTurn(text="checking now", tool_calls=[ToolCall(id="t1", name="echo", input={})]),
        FakeTurn(text="done"),
    ]
    graph = build_agentic_graph(fake_llm_client(script=script), [_ECHO_TOOL])

    final = await graph.ainvoke(_state())

    # Summed across both call_model invocations ("checking now" + "done"), not just the last.
    assert final["usage"].output_tokens == len("checking now".split()) + len("done".split())
