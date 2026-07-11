"""LangGraph state for the tutor turn and the refinement gate.

Checkpoint-serializable by construction — only Pydantic/primitive fields, never an
AsyncSession, ORM row, or LLMClient.
"""

from typing import TypedDict

from app.llm.types import ChatMessage, Usage


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
