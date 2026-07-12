"""The guided-practice workflow as a LangGraph HITL subgraph.

Loop: ``present`` (LLM presents a worked example + practice problem, streaming tokens) ->
``await_response`` (pauses via ``interrupt()`` until the learner attempts it) -> ``grade``
(grades the attempt through the existing answer->tracer->plan-revise transaction) -> ``respond``
(LLM gives feedback, streaming tokens) -> conditional: correct or out of rounds -> ``END``, else
back to ``await_response``.

Unlike the plain tutor graph, this one is compiled **with a checkpointer** — the pause/resume
across HTTP requests requires LangGraph to persist state between the interrupt and its resume
(same mechanism as ``app/agent/refinement.py``, whose module docstring documents the
in-memory-checkpointer limitations this graph shares). Unlike refinement, ``grade`` also writes
to the DB mid-graph — it closes over ``session``/``learner_id`` (passed into
``build_workflow_graph``, never stored in state, which must stay checkpoint-serializable). The
dispatcher (``app/services/workflow.py``) rebuilds the graph fresh each request with that
request's own ``session``/``llm``, exactly like ``build_refinement_graph(llm)`` already is.
"""

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import WorkflowState
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as assessment_svc

__all__ = ["WorkflowState", "build_workflow_graph", "workflow_config"]

_CHECKPOINTER = InMemorySaver()


def workflow_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def build_workflow_graph(
    llm: LLMClient, session: AsyncSession, *, learner_id: uuid.UUID
) -> CompiledStateGraph[Any, Any, Any, Any]:
    async def _stream(messages: list[ChatMessage], system: str, max_tokens: int) -> dict[str, Any]:
        """Shared body for ``present``/``respond`` — one SMART call, streamed."""
        writer = get_stream_writer()
        parts: list[str] = []
        usage = Usage()
        async for chunk in llm.stream(
            ModelRole.SMART, messages, system=system, max_tokens=max_tokens
        ):
            if chunk.text:
                parts.append(chunk.text)
                writer({"token": chunk.text})
            if chunk.usage is not None:
                usage = chunk.usage
        text = "".join(parts)
        new_messages = [*messages, ChatMessage(role=ChatRole.ASSISTANT, content=text)]
        return {"messages": new_messages, "last_message": text, "usage": usage}

    async def present(state: WorkflowState) -> dict[str, Any]:
        return await _stream(state["messages"], state["system"], state["max_tokens"])

    async def await_response(state: WorkflowState) -> dict[str, Any]:
        reply = interrupt({"prompt": state["last_message"], "round": state["rounds"] + 1})
        response_text = reply.get("response_text", "")
        messages = [*state["messages"], ChatMessage(role=ChatRole.USER, content=response_text)]
        return {"messages": messages, "response_text": response_text}

    async def grade(state: WorkflowState) -> dict[str, Any]:
        item = await assessment_svc.get_item(session, uuid.UUID(state["item_id"]))
        if item is None:
            return {"score": 0.0, "correct": False, "rounds": state["rounds"] + 1}
        result, _states = await assessment_svc.answer_item(
            session,
            learner_id,
            item,
            AnswerSubmit(response={"text": state["response_text"]}),
            llm=llm,
        )
        return {"score": result.score, "correct": result.correct, "rounds": state["rounds"] + 1}

    async def respond(state: WorkflowState) -> dict[str, Any]:
        outcome = "correct" if state["correct"] else "incorrect"
        note = f"The learner's last attempt scored {state['score']:.2f} ({outcome})."
        continuing = not (state["correct"] or state["rounds"] >= state["max_rounds"])
        instruction = (
            "Give brief, encouraging feedback on the attempt above, referencing what they got "
            "right or wrong."
        )
        if continuing:
            instruction += (
                " Then remind them of the same practice problem and offer a hint — do not pose "
                "a different question, since their next answer is graded against this same one."
            )
        else:
            instruction += " Then wrap up warmly."
        system = f"{state['system']}\n\n{note} {instruction}"
        return await _stream(state["messages"], system, state["max_tokens"])

    def route_after_respond(state: WorkflowState) -> str:
        if state["correct"] or state["rounds"] >= state["max_rounds"]:
            return "end"
        return "await_response"

    graph = StateGraph(WorkflowState)  # ty: ignore[invalid-argument-type]
    graph.add_node("present", present)
    graph.add_node("await_response", await_response)
    graph.add_node("grade", grade)
    graph.add_node("respond", respond)
    graph.add_edge(START, "present")
    graph.add_edge("present", "await_response")
    graph.add_edge("await_response", "grade")
    graph.add_edge("grade", "respond")
    graph.add_conditional_edges(
        "respond", route_after_respond, {"end": END, "await_response": "await_response"}
    )
    return graph.compile(checkpointer=_CHECKPOINTER)
