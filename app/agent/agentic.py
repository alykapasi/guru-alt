"""The agentic turn as a bounded tool-calling LangGraph.

`build_agentic_graph(llm, tools)` compiles a ``call_model`` <-> ``execute_tools`` loop,
bounded by ``max_iterations``, with no checkpointer — same no-HITL shape as
``app/agent/tutor.py``. ``call_model`` mirrors ``tutor.py``'s ``generate`` node (stream via
the role-based ``LLMClient``, tokens over the custom stream writer) plus ``tools=`` and
tool-call accumulation; ``execute_tools`` runs each pending call and never lets a tool's
own failure crash the turn.
"""

from collections.abc import Sequence
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.state import AgenticState, ToolEvent
from app.agent.tools import Tool
from app.llm.registry import LLMClient
from app.llm.types import (
    ChatMessage,
    ChatRole,
    ContentPart,
    ModelRole,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    Usage,
)

__all__ = ["AgenticState", "ToolEvent", "build_agentic_graph"]


def build_agentic_graph(
    llm: LLMClient, tools: Sequence[Tool]
) -> CompiledStateGraph[Any, Any, Any, Any]:
    tool_defs = [t.to_def() for t in tools]
    by_name = {t.name: t for t in tools}

    async def call_model(state: AgenticState) -> dict[str, Any]:
        writer = get_stream_writer()
        parts: list[str] = []
        usage = state["usage"]
        tool_calls: list[ToolCall] = []
        async for chunk in llm.stream(
            ModelRole.SMART,
            state["messages"],
            system=state["system"],
            max_tokens=state["max_tokens"],
            tools=tool_defs,
        ):
            if chunk.text:
                parts.append(chunk.text)
                writer({"token": chunk.text})
            if chunk.usage is not None:
                usage = Usage(
                    input_tokens=usage.input_tokens + chunk.usage.input_tokens,
                    output_tokens=usage.output_tokens + chunk.usage.output_tokens,
                )
            if chunk.tool_calls is not None:
                tool_calls = chunk.tool_calls

        reply = "".join(parts)
        content: list[ContentPart] = []
        if reply:
            content.append(TextPart(text=reply))
        content.extend(ToolUsePart(id=tc.id, name=tc.name, input=tc.input) for tc in tool_calls)
        messages = state["messages"]
        if content:
            messages = [*messages, ChatMessage(role=ChatRole.ASSISTANT, content=content)]

        return {
            "messages": messages,
            "reply": reply,
            "usage": usage,
            "pending_tool_calls": tool_calls,
        }

    async def execute_tools(state: AgenticState) -> dict[str, Any]:
        writer = get_stream_writer()
        messages = list(state["messages"])
        events = list(state["tool_events"])
        for call in state["pending_tool_calls"]:
            tool = by_name.get(call.name)
            if tool is None:
                result_content, is_error = f"Unknown tool: {call.name}", True
            else:
                try:
                    result = await tool.execute(call.input)
                    result_content, is_error = result.content, result.is_error
                except Exception as exc:  # a tool's own failure must not crash the turn
                    result_content, is_error = f"Tool failed: {exc}", True
            messages.append(
                ChatMessage(
                    role=ChatRole.TOOL,
                    content=[
                        ToolResultPart(
                            tool_use_id=call.id, content=result_content, is_error=is_error
                        )
                    ],
                )
            )
            events.append(
                ToolEvent(
                    name=call.name, input=call.input, result=result_content, is_error=is_error
                )
            )
            writer({"tool_call": call.name})
        return {
            "messages": messages,
            "tool_events": events,
            "iterations": state["iterations"] + 1,
            "pending_tool_calls": [],
        }

    def _route(state: AgenticState) -> str:
        if state["pending_tool_calls"] and state["iterations"] < state["max_iterations"]:
            return "execute_tools"
        return "end"

    graph = StateGraph(AgenticState)  # ty: ignore[invalid-argument-type]
    graph.add_node("call_model", call_model)
    graph.add_node("execute_tools", execute_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges(
        "call_model", _route, {"execute_tools": "execute_tools", "end": END}
    )
    graph.add_edge("execute_tools", "call_model")
    return graph.compile()
