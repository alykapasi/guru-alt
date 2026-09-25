"""Request and response bodies for concept links (S24)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LinkSideRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kc_id: uuid.UUID
    kc_name: str
    subject_id: uuid.UUID
    subject_name: str


class ConceptLinkSuggestionRead(BaseModel):
    """An endorsed link the learner can accept (``decision`` null) or revoke (``"accepted"``)."""

    model_config = ConfigDict(from_attributes=True)

    link_id: uuid.UUID
    reason: str | None
    endorsed_by: Literal["admin", "judge"] | None
    decision: Literal["accepted"] | None
    a: LinkSideRead
    b: LinkSideRead


class ConceptLinkDecisionSubmit(BaseModel):
    decision: Literal["accept", "decline", "revoke"]


class ConceptLinkReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kc_a_id: uuid.UUID
    kc_b_id: uuid.UUID
    kc_a_name: str
    kc_b_name: str
    subject_a_name: str
    subject_b_name: str
    verdict: Literal["endorsed", "rejected"] | None
    reason: str | None
    decided_at: datetime | None


class ConceptLinkVerdictSubmit(BaseModel):
    endorse: bool
    reason: str = Field(min_length=1, max_length=500)
