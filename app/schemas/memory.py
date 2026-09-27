"""Request/response schemas for per-learner memory."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReplacedRead(BaseModel):
    """The memory a current one replaced (S42) — shown so a wrong replacement can be undone."""

    id: uuid.UUID
    content: str


class MemoryRead(BaseModel):
    """A stored memory as exposed to the learner — not the raw embedding."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    content: str
    conversation_id: uuid.UUID | None
    created_at: datetime
    # Where it was learned (S42): the conversation id even after that conversation is deleted,
    # its title while it exists, and whether it still does.
    origin_conversation_id: uuid.UUID | None = None
    origin_title: str | None = None
    origin_live: bool = False
    # What this memory replaced, if anything (S42).
    replaced: ReplacedRead | None = None


class ForgetOriginRead(BaseModel):
    forgotten: int


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
