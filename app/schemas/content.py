"""Request/response schemas for the content engine."""

import uuid

from pydantic import BaseModel, ConfigDict

from app.models.content import ContentType


class GenerateRequest(BaseModel):
    """Generate content for a KC. With ``type`` omitted, the default block set is assembled."""

    kc_id: uuid.UUID
    type: ContentType | None = None


class ContentBlockRead(BaseModel):
    """A generated, KC-tagged content block with its grounding citations."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kc_ids: list[uuid.UUID]
    block_type: str
    body: str
    citations: list[dict]
    model: str
