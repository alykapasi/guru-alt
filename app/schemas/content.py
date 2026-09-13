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
    # How many chunks were offered as grounding (S28). ``0`` means the block was written from
    # general knowledge because retrieval found nothing — which an empty ``citations`` alone
    # cannot say, since a model given snippets may cite none of them. ``None`` means the block
    # predates the column and nothing was recorded.
    grounding_count: int | None = None
