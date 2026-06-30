"""Request/response schemas for chat."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    learner_id: uuid.UUID
    title: str | None
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
