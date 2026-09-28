"""Request/response schemas for learner export and deletion (S61)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class StoreRetentionRead(BaseModel):
    table: str
    disposition: str  # deleted | anonymised | retained | partly deleted
    reason: str


class RetentionPolicyRead(BaseModel):
    stores: list[StoreRetentionRead]


class DeletionReportRead(BaseModel):
    learner_id: uuid.UUID
    blobs_deleted: int
    # Left in place because another learner independently uploaded the same bytes.
    blobs_retained: int
    # A count, not the keys: a key names an object the caller has no further use for, and
    # echoing storage paths back over the API is a detail worth not publishing.
    blobs_failed: int
    items_deleted: int
    # False when object storage refused something. The database rows are gone either way.
    complete: bool


class DeletionRequestRead(BaseModel):
    """When a pending account will be erased, unless it is restored first (S61)."""

    due_at: datetime
    # True when the request asked to erase at once (``?now=true``) and it has been.
    erased: bool = False


class DeletionStatusRead(BaseModel):
    pending: bool
    requested_at: datetime | None
    due_at: datetime | None


class ExportFileRead(BaseModel):
    """One uploaded file, and where to download it (S61)."""

    id: uuid.UUID
    origin: str
    file_path: str
