"""Request/response schemas for per-learner memory."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryRead(BaseModel):
    """A stored memory as exposed to the learner — not the raw embedding."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content: str
    conversation_id: uuid.UUID | None
    created_at: datetime


class MemoryCorrection(BaseModel):
    """What the learner says is actually true (S16).

    Only the text. The kind stays as extracted, and the provenance of the *correction* is the
    learner rather than a conversation — letting a client restate either would be letting it
    describe where a belief came from, which is the part that has to be the system's own
    record.
    """

    content: str = Field(min_length=1, max_length=2000)


class WriteBackAck(BaseModel):
    """Acknowledges a queued write-back job — the created memories aren't returned
    synchronously; see ``GET /memory`` once the job has run."""

    status: Literal["queued"] = "queued"
