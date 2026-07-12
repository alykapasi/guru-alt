"""LangGraph state for the tutor turn and the refinement gate.

Checkpoint-serializable by construction — only Pydantic/primitive fields, never an
AsyncSession, ORM row, or LLMClient.
"""

from typing import Any, TypedDict

from pydantic import BaseModel

from app.llm.types import ChatMessage, ToolCall, Usage


class TutorState(TypedDict):
    messages: list[ChatMessage]  # full context incl. the new user turn
    system: str
    max_tokens: int
    reply: str  # filled by the generate node
    usage: Usage  # filled by the generate node


class RefinementState(TypedDict):
    """State for the interactive prompt-refinement gate (HITL loop).

    ``messages`` grows each round: the learner's raw goal, each proposal, each round of
    feedback. Must stay checkpoint-serializable across the ``ask_learner`` interrupt.
    """

    messages: list[ChatMessage]
    system: str
    max_tokens: int
    max_rounds: int
    proposal: str  # filled by the propose node each round
    usage: Usage  # filled by the propose node each round
    rounds: int  # rounds completed (incremented by ask_learner on resume)
    satisfied: bool  # explicit learner acceptance
    auto_committed: bool  # true if committed by hitting max_rounds, not explicit acceptance
    agreed_goal: str  # filled by the commit node


class ToolEvent(BaseModel):
    """One tool-execution result — the "tool_call" SSE event's payload + a test-friendly trace."""

    name: str
    input: dict[str, Any]
    result: str
    is_error: bool


class WorkflowState(TypedDict):
    """State for the guided-practice workflow graph (present -> await_response -> grade ->
    respond, looping until correct or capped).

    ``item_id`` is stored as ``str(uuid.UUID)``, never a UUID object or ORM row — the same
    checkpoint-serializable-primitives rule as everything else in this module.
    """

    messages: list[ChatMessage]
    system: str
    max_tokens: int
    item_id: str
    response_text: str  # the learner's latest attempt, filled by await_response on resume
    last_message: str  # this round's full presented/feedback text (present's or respond's)
    score: float  # filled by grade
    correct: bool  # filled by grade
    usage: Usage  # this call's LLM usage (present's or respond's — grade's own call self-logs)
    rounds: int  # graded attempts completed so far
    max_rounds: int


class AgenticState(TypedDict):
    """State for the bounded tool-calling loop (call_model <-> execute_tools).

    No checkpointer, like ``TutorState`` — a single read-only tool needs no HITL pause. A
    future side-effecting tool wanting an approval gate would add one the way
    ``RefinementState``'s ``ask_learner`` interrupt does, without touching this shape.
    """

    messages: list[ChatMessage]
    system: str
    max_tokens: int
    max_iterations: int  # cap on tool-execution rounds this turn
    iterations: int  # tool-execution rounds completed so far
    reply: str  # filled by call_model each pass
    usage: Usage  # accumulated across every call_model invocation this turn
    pending_tool_calls: list[ToolCall]  # set by call_model, drained by execute_tools
    tool_events: list[ToolEvent]  # append-only trace
