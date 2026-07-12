"""Request/response schemas for the learner profile."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DimensionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    kind: str
    source: str
    value: Any
    uncertainty: float
    updated_at: datetime


class ProfileSnapshotRead(BaseModel):
    dimensions: list[DimensionRead]
