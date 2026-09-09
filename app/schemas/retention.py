"""Request/response schemas for learner export and deletion (S61)."""

import uuid

from pydantic import BaseModel


class StoreRetentionRead(BaseModel):
    table: str
    disposition: str  # deleted | anonymised | retained
    reason: str


class RetentionPolicyRead(BaseModel):
    stores: list[StoreRetentionRead]


class DeletionReportRead(BaseModel):
    learner_id: uuid.UUID
    blobs_deleted: int
    # A count, not the keys: a key names an object the caller has no further use for, and
    # echoing storage paths back over the API is a detail worth not publishing.
    blobs_failed: int
    items_deleted: int
    # False when object storage refused something. The database rows are gone either way.
    complete: bool
