"""Request/response schemas for the content engine."""

import uuid

from pydantic import BaseModel, ConfigDict, computed_field

from app.models.content import ContentType
from app.rag import coverage as coverage_rules
from app.rag.coverage import Coverage


class GenerateRequest(BaseModel):
    """Generate content for a KC. With ``type`` omitted, the default block set is assembled."""

    kc_id: uuid.UUID
    type: ContentType | None = None


class ClaimVerdictRead(BaseModel):
    """One checkable claim from a block, against the passages offered for it."""

    model_config = ConfigDict(from_attributes=True)

    claim: str
    verdict: str
    supported_by: list[int]
    reason: str


class SupportReportRead(BaseModel):
    """Whether a block's citations actually carry what it says (S28).

    ``supported_fraction`` is null when no claim could be extracted — not 1.0, which would read
    as a clean bill of health for a block nobody managed to check.
    """

    model_config = ConfigDict(from_attributes=True)

    claims: list[ClaimVerdictRead]
    contradictions: list[tuple[int, int]]
    supported_fraction: float | None
    clean: bool


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

    @computed_field
    @property
    def coverage(self) -> Coverage | None:
        """How much of this the learner's sources carried (S28), from what was recorded."""
        return coverage_rules.coverage(self.grounding_count, self.citations)

    @computed_field
    @property
    def cited_source_count(self) -> int:
        return coverage_rules.cited_source_count(self.citations)
