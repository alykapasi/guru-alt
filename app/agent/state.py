"""LangGraph state for the tutor turn.

Checkpoint-serializable by construction — only Pydantic/primitive fields, never an
AsyncSession, ORM row, or LLMClient. Grows (goal, pending_question, kc_ids) in later slices.
"""

from typing import TypedDict

from app.llm.types import ChatMessage, Usage


class TutorState(TypedDict):
    messages: list[ChatMessage]  # full context incl. the new user turn
    system: str
    max_tokens: int
    reply: str  # filled by the generate node
    usage: Usage  # filled by the generate node
