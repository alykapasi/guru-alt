"""What could not be erased is retried until it is (S61, V12)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.identity import FakeIdentityProvider
from app.models.erasure import ErasureKind, PendingErasure
from app.services import retention
from app.storage import InMemoryBlobStore


class _Refusing(InMemoryBlobStore):
    def __init__(self) -> None:
        super().__init__()
        self.refuse = True

    async def delete(self, key: str) -> None:
        if self.refuse:
            raise RuntimeError("store unavailable")
        await super().delete(key)


async def _row(session: AsyncSession, kind: str, target: str) -> PendingErasure | None:
    return await session.scalar(
        select(PendingErasure)
        .where(PendingErasure.kind == kind, PendingErasure.target == target)
        .execution_options(populate_existing=True)
    )


async def test_a_refused_blob_is_retried_with_backoff_then_deleted(
    db_session: AsyncSession,
) -> None:
    store = _Refusing()
    key = f"blobs/{uuid.uuid4().hex}"
    await store.put(key, b"bytes")
    await retention.queue_erasure(db_session, ErasureKind.BLOB, key, "refused")
    now = datetime.now(UTC) + timedelta(seconds=1)

    assert await retention.retry_erasures(db_session, store, None, now=now) == 0
    first = await _row(db_session, "blob", key)
    assert first is not None and first.attempts == 1 and first.next_attempt_at > now
    later = first.next_attempt_at + timedelta(seconds=1)
    store.refuse = False

    assert await retention.retry_erasures(db_session, store, None, now=later) == 1
    assert await _row(db_session, "blob", key) is None
    assert not await store.exists(key)


async def test_a_key_someone_uploads_again_is_left_alone(db_session: AsyncSession) -> None:
    from app.models.learner import Learner
    from app.models.source import Source, SourceKind, SourceStatus

    store = InMemoryBlobStore()
    key = f"blobs/{uuid.uuid4().hex}"
    await store.put(key, b"bytes")
    await retention.queue_erasure(db_session, ErasureKind.BLOB, key, "refused")
    other = Learner(handle=f"o-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Source(
            learner_id=other.id,
            kind=SourceKind.FILE,
            origin="x.txt",
            status=SourceStatus.DONE,
            blob_key=key,
            meta={},
            attempts=0,
        )
    )
    await db_session.commit()

    await retention.retry_erasures(
        db_session, store, None, now=datetime.now(UTC) + timedelta(seconds=1)
    )

    assert await store.exists(key)
    assert await _row(db_session, "blob", key) is None


async def test_an_identity_is_retried_and_already_gone_counts_as_done(
    db_session: AsyncSession,
) -> None:
    provider = FakeIdentityProvider()
    user = provider.add_user(emails=["x@example.com"])
    await retention.queue_erasure(db_session, ErasureKind.IDENTITY, user.subject, "refused")

    resolved = await retention.retry_erasures(
        db_session, InMemoryBlobStore(), provider, now=datetime.now(UTC) + timedelta(seconds=1)
    )

    assert resolved == 1 and user.subject in provider.deleted


async def test_stuck_erasures_raise_an_alert(db_session: AsyncSession) -> None:
    from app.core.alerts import evaluate
    from tests.test_ops_signals import _backlog, _ready, _spend

    key = f"blobs/{uuid.uuid4().hex}"
    await retention.queue_erasure(db_session, ErasureKind.BLOB, key, "refused")
    row = await _row(db_session, "blob", key)
    assert row is not None
    row.attempts = 10
    await db_session.commit()

    stuck = await retention.stuck_erasures(db_session, attempts=10)
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(),
        spend=_spend(),
        settings=Settings(),
        stuck_erasures=stuck,
    )

    assert stuck >= 1
    assert "erasures_stuck" in [a.name for a in report.firing]
