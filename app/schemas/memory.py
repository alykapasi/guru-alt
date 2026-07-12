"""Request/response schemas for per-learner memory."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MemoryRead(BaseModel):
    """A stored memory as exposed to the learner — not the raw embedding."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content: str
    conversation_id: uuid.UUID | None
    created_at: datetime


class WriteBackAck(BaseModel):
    """Acknowledges a queued write-back job — the created memories aren't returned
    synchronously; see ``GET /memory`` once the job has run."""

    status: Literal["queued"] = "queued"
