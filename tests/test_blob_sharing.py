"""Identical uploads are stored once, and one account closing does not take another's bytes.

Blob keys are content-addressed (``blobs/<sha256>``), so two learners who independently upload
the same file write the same key and the store holds one object. Nothing *derived* is shared —
each learner gets their own source, extraction, chunks and embeddings — and neither can observe
that the other references it.

The deletion consequence is the load-bearing part: S61 promises an account's bytes are
destroyed, and a shared key means "destroy" now has to mean "if nobody else is using it".
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.models.source import Source, SourceKind
from app.services import ingestion as svc
from app.services import retention as retention_svc
from app.storage.base import BlobNotFound
from app.storage.memory import InMemoryBlobStore

TEXTBOOK = b"Photosynthesis converts light energy into chemical energy."


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _upload(session: AsyncSession, blobstore: InMemoryBlobStore, learner: Learner) -> Source:
    return await svc.create_source(
        session,
        blobstore,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="biology.pdf",
        content_type="application/pdf",
        data=TEXTBOOK,
    )


async def test_two_learners_uploading_the_same_file_store_one_object(
    db_session: AsyncSession,
) -> None:
    blobstore = InMemoryBlobStore()
    first = await _upload(db_session, blobstore, await _learner(db_session))
    second = await _upload(db_session, blobstore, await _learner(db_session))

    assert first.blob_key == second.blob_key
    assert first.id != second.id  # separate sources; only the bytes are shared
    assert len(blobstore._store) == 1  # one object, two sources


async def test_the_key_carries_no_learner_or_source(db_session: AsyncSession) -> None:
    blobstore = InMemoryBlobStore()
    source = await _upload(db_session, blobstore, await _learner(db_session))

    key = source.blob_key
    assert key is not None  # create_source always writes one
    assert key == f"blobs/{source.content_sha256}"
    assert str(source.learner_id) not in key
    assert str(source.id) not in key


async def test_closing_one_account_leaves_the_other_learners_bytes(
    db_session: AsyncSession,
) -> None:
    """The whole risk of sharing a key: 'delete this learner's uploads' must not mean
    'delete the file somebody else independently uploaded'."""
    blobstore = InMemoryBlobStore()
    leaving = await _learner(db_session)
    staying = await _learner(db_session)
    await _upload(db_session, blobstore, leaving)
    kept = await _upload(db_session, blobstore, staying)
    kept_key = kept.blob_key
    assert kept_key is not None

    report = await retention_svc.delete_learner(db_session, blobstore, leaving.id)

    assert report.blobs_deleted == 0
    assert report.blobs_retained == 1
    assert report.complete
    assert await blobstore.get(kept_key) == TEXTBOOK


async def test_the_last_account_to_leave_takes_the_bytes_with_it(
    db_session: AsyncSession,
) -> None:
    blobstore = InMemoryBlobStore()
    first = await _learner(db_session)
    second = await _learner(db_session)
    source = await _upload(db_session, blobstore, first)
    await _upload(db_session, blobstore, second)
    key = source.blob_key
    assert key is not None

    await retention_svc.delete_learner(db_session, blobstore, first.id)
    report = await retention_svc.delete_learner(db_session, blobstore, second.id)

    assert report.blobs_deleted == 1
    with pytest.raises(BlobNotFound):
        await blobstore.get(key)


async def test_a_failed_upload_does_not_delete_bytes_someone_else_is_using(
    db_session: AsyncSession,
) -> None:
    """The cleanup path for an upload whose row never commits used to delete the key it wrote.
    Content-addressed, that key may already be another learner's only copy."""
    blobstore = InMemoryBlobStore()
    keeper = await _upload(db_session, blobstore, await _learner(db_session))
    keeper_key = keeper.blob_key
    assert keeper_key is not None

    deleted = await svc.unreference_blob(db_session, blobstore, keeper_key)

    assert deleted is False
    assert await blobstore.get(keeper_key) == TEXTBOOK
