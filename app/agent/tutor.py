"""The tutor turn as a single-node LangGraph state graph.

`build_tutor_graph(llm)` closes over the request's LLM client (kept out of state, which
must stay serializable) and compiles START -> generate -> END with no checkpointer (this
turn is not HITL). The generate node streams tokens over the custom stream writer while
accumulating the full reply + final usage.
"""

from typing import Any

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.state import TutorState
from app.llm.registry import LLMClient
from app.llm.types import ModelRole, Usage

__all__ = ["TutorState", "build_tutor_graph"]


def build_tutor_graph(llm: LLMClient) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def generate(state: TutorState) -> dict[str, Any]:
        writer = get_stream_writer()
        parts: list[str] = []
        usage = Usage()
        async for chunk in llm.stream(
            ModelRole.SMART,
            state["messages"],
            system=state["system"],
            max_tokens=state["max_tokens"],
        ):
            if chunk.text:
                parts.append(chunk.text)
                writer({"token": chunk.text})
            if chunk.usage is not None:
                usage = chunk.usage
        return {"reply": "".join(parts), "usage": usage}

    graph = StateGraph(TutorState)  # ty: ignore[invalid-argument-type]
    graph.add_node("generate", generate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    return graph.compile()
