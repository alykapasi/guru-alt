"""Does the object store still hold what the database says it does? (S60)

The database dump does not contain the uploaded bytes. Blobs live in the object store and are
shared by content hash (S77), so a restore that brings back the database and not the bucket
produces something worse than an obvious failure: every `Source` row is present, the library
looks intact, and the bytes behind each one are gone — and because they were content-addressed
and de-duplicated, they cannot be re-derived from anything else in the backup.

That makes "is the restore valid?" a question with a concrete answer, and this is it: walk
every referenced key and ask the store whether it is there. It is the check to run *after* a
restore and before letting learners back in, and on a schedule to catch a bucket lifecycle rule
quietly expiring objects the database still points at.

The reverse direction — objects nothing references — is deliberately not here. Those are a
cleanup question, not a correctness one, and deleting them safely is the orphan reconciliation
S61 owns.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source import Source
from app.storage.base import BlobStore


class MissingBlob(BaseModel):
    """A source whose bytes are not in the store."""

    source_id: uuid.UUID
    learner_id: uuid.UUID | None
    origin: str
    blob_key: str


class IntegrityReport(BaseModel):
    """What the walk found."""

    checked: int
    missing: list[MissingBlob]

    @property
    def intact(self) -> bool:
        return not self.missing


async def check(
    session: AsyncSession, store: BlobStore, *, limit: int | None = None
) -> IntegrityReport:
    """Verify every blob the database references is present in ``store``.

    Sources with no ``blob_key`` are skipped rather than reported: a URL source that was
    fetched and parsed has no stored object by design, and reporting those as missing would
    bury the real failures under rows that are working correctly.
    """
    statement = select(Source).where(Source.blob_key.is_not(None)).order_by(Source.created_at)
    if limit is not None:
        statement = statement.limit(limit)

    missing: list[MissingBlob] = []
    checked = 0
    for source in (await session.scalars(statement)).all():
        key = source.blob_key
        if not key:
            continue
        checked += 1
        if not await store.exists(key):
            missing.append(
                MissingBlob(
                    source_id=source.id,
                    learner_id=source.learner_id,
                    origin=source.origin,
                    blob_key=key,
                )
            )
    return IntegrityReport(checked=checked, missing=missing)
