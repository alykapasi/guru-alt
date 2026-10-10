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
    # What this dimension is, in the learner's words, and what was actually observed to
    # produce it (S44). Filled from the catalog rather than the row — see
    # ``app.learning.profile_estimators.describe``. Without these a client has only the key,
    # and a title-cased key states a finding the measurement does not support.
    label: str = ""
    observation: str = ""
    # Estimated from what the learner typed, and not updated while memory is paused (O07).
    paused: bool = False


def to_read(dimension: Any, *, memory_paused: bool = False) -> DimensionRead:
    """One stored dimension, described. Import-local to avoid a schema->learning import at
    module scope; the catalog is the single source of truth for what a key means."""
    from app.learning.profile_estimators import describe, reads_messages

    read = DimensionRead.model_validate(dimension)
    read.label, read.observation = describe(read.key)
    read.paused = memory_paused and reads_messages(read.key)
    return read


class ProfileSnapshotRead(BaseModel):
    dimensions: list[DimensionRead]
