"""Request/response schemas for chat."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = None
    # "session" conversations (created by the Lessons page's "Start practice") always run the
    # guided-practice workflow and are never shown behind the plain chat gate/composer — see
    # Conversation.kind. Set once at creation, never changed.
    kind: Literal["chat", "session"] = "chat"
    # Set once at creation, never changed. Scopes plan grounding + the session runner's
    # practice item to this exact subject instead of a cross-subject heuristic. Also the hard
    # retrieval/citation boundary (Phase 7): None = "general", no library grounding at all.
    subject_id: uuid.UUID | None = None
    # Narrows retrieval to specific sources within `subject_id`'s materials — empty means "all
    # sources under this subject." Must be empty when subject_id is None (nothing to narrow).
    source_ids: list[uuid.UUID] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    learner_id: uuid.UUID
    title: str | None
    kind: str
    goal: str | None
    subject_id: uuid.UUID | None
    source_ids: list[uuid.UUID]
    created_at: datetime


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    model: str | None
    # Each entry: {"marker": int, "chunk_id": str, "source_id": str} — see Message.citations.
    citations: list[dict]
    created_at: datetime


class ChatTurnRequest(BaseModel):
    content: str = Field(min_length=1)
    # Explicit learner acceptance of the refinement gate's latest proposal. Ignored once a
    # conversation's goal is already committed (or no gate is in progress).
    satisfied: bool = False
    # "agentic" is a one-off tool-using action for this turn only — it bypasses the
    # refinement gate regardless of the conversation's goal/gate state. "workflow" starts (or,
    # if already sent, is overridden by) the guided-practice workflow, which likewise
    # presupposes a committed goal + plan and so also bypasses the gate. Per-turn rather than
    # persisted on the conversation: no migration, and a learner can mix one tool-using or
    # workflow turn into an otherwise plain conversation.
    mode: Literal["chat", "agentic", "workflow"] = "chat"
