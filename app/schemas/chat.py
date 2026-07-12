"""Request/response schemas for chat."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = None
    # Set once at creation, never changed. Scopes plan grounding + the session runner's
    # practice item to this exact subject instead of a cross-subject heuristic.
    subject_id: uuid.UUID | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    learner_id: uuid.UUID
    title: str | None
    goal: str | None
    subject_id: uuid.UUID | None
    created_at: datetime


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    model: str | None
    created_at: datetime


class ChatTurnRequest(BaseModel):
    content: str = Field(min_length=1)
    # Explicit learner acceptance of the refinement gate's latest proposal. Ignored once a
    # conversation's goal is already committed (or no gate is in progress).
    satisfied: bool = False
