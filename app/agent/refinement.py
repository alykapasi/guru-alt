"""The interactive prompt-refinement gate as a LangGraph HITL subgraph.

Loop: ``propose`` (LLM proposes/refines a scoped learning goal, streaming tokens) ->
``ask_learner`` (pauses via ``interrupt()`` until the learner explicitly accepts or gives
feedback) -> conditional: satisfied or out of rounds -> ``commit``, else back to ``propose``.

Unlike the plain tutor graph, this one is compiled **with a checkpointer** — the pause/resume
across HTTP requests requires LangGraph to persist state between the interrupt and its resume.
``_CHECKPOINTER`` is a process-wide ``InMemorySaver``: state does not survive a process restart
and is not shared across workers. Acceptable for single-process Phase 5; revisit with a durable
(e.g. Postgres) checkpointer before horizontal scaling (Phase 8). The dispatcher that calls this
graph (``app/services/refinement.py``) is written to degrade gracefully if state is lost, rather
than assume it's always present.
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.agent.state import RefinementState
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage

__all__ = ["RefinementState", "build_refinement_graph", "refinement_config"]

_CHECKPOINTER = InMemorySaver()


def refinement_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def build_refinement_graph(llm: LLMClient) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def propose(state: RefinementState) -> dict[str, Any]:
        writer = get_stream_writer()
        parts: list[str] = []
        usage = Usage()
        async for chunk in llm.stream(
            ModelRole.FAST,
            state["messages"],
            system=state["system"],
            max_tokens=state["max_tokens"],
        ):
            if chunk.text:
                parts.append(chunk.text)
                writer({"token": chunk.text})
            if chunk.usage is not None:
                usage = chunk.usage
        proposal = "".join(parts)
        messages = [*state["messages"], ChatMessage(role=ChatRole.ASSISTANT, content=proposal)]
        return {"messages": messages, "proposal": proposal, "usage": usage}

    async def ask_learner(state: RefinementState) -> dict[str, Any]:
        reply = interrupt({"proposal": state["proposal"], "round": state["rounds"] + 1})
        messages = state["messages"]
        feedback = reply.get("feedback", "")
        if feedback:
            messages = [*messages, ChatMessage(role=ChatRole.USER, content=feedback)]
        return {
            "messages": messages,
            "rounds": state["rounds"] + 1,
            "satisfied": bool(reply.get("satisfied", False)),
        }

    def route_after_ask(state: RefinementState) -> str:
        if state["satisfied"] or state["rounds"] >= state["max_rounds"]:
            return "commit"
        return "propose"

    async def commit(state: RefinementState) -> dict[str, Any]:
        return {
            "agreed_goal": state["proposal"],
            "auto_committed": not state["satisfied"],
        }

    graph = StateGraph(RefinementState)  # ty: ignore[invalid-argument-type]
    graph.add_node("propose", propose)
    graph.add_node("ask_learner", ask_learner)
    graph.add_node("commit", commit)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "ask_learner")
    graph.add_conditional_edges(
        "ask_learner", route_after_ask, {"propose": "propose", "commit": "commit"}
    )
    graph.add_edge("commit", END)
    return graph.compile(checkpointer=_CHECKPOINTER)
