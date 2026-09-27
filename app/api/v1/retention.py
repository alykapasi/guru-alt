"""Export and delete everything held about the current learner (S61).

Retention was implicit in a dozen scattered ``ondelete`` clauses, with no way for a learner to
get their data out or have it removed, and two stores no foreign key reaches. See
``app.services.retention`` for the policy these two endpoints execute.
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.api.deps import (
    AccountHolder,
    BlobStoreDep,
    CurrentLearner,
    IdentityProviderDep,
    SessionDep,
    SettingsDep,
)
from app.models.source import Source
from app.schemas.retention import (
    DeletionReportRead,
    DeletionRequestRead,
    DeletionStatusRead,
    ExportFileRead,
    RetentionPolicyRead,
    StoreRetentionRead,
)
from app.services import retention as svc

router = APIRouter(tags=["retention"])


@router.get("/me/retention", response_model=RetentionPolicyRead)
async def retention_policy(_: CurrentLearner):
    """What happens to each store when an account is deleted, and why.

    Published rather than documented: a learner deciding whether to delete an account should
    be able to read the policy the code actually executes.
    """
    return RetentionPolicyRead(
        stores=[
            StoreRetentionRead(table=e.table, disposition=e.disposition, reason=e.reason)
            for e in svc.RETENTION
        ]
    )


@router.get("/me/export")
async def export_me(session: SessionDep, learner: AccountHolder) -> JSONResponse:
    """Everything held about this learner, as JSON. Each upload's bytes download separately,
    from the ``file_path`` on its source entry."""
    try:
        exported = await svc.export_learner(session, learner.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "learner not found") from exc
    # An attachment, so a link to it saves a file rather than opening a wall of JSON.
    return JSONResponse(
        jsonable_encoder(exported),
        headers={"Content-Disposition": 'attachment; filename="guru-export.json"'},
    )


@router.get("/me/export/files", response_model=list[ExportFileRead])
async def export_files(session: SessionDep, learner: AccountHolder):
    """Every file this learner uploaded, each with its download path (S61)."""
    return [
        ExportFileRead(id=source.id, origin=source.origin, file_path=svc.file_path(source.id))
        for source in await svc.export_files(session, learner.id)
    ]


@router.get("/me/export/sources/{source_id}/file")
async def export_source_file(
    source_id: uuid.UUID, session: SessionDep, learner: AccountHolder, blobstore: BlobStoreDep
) -> Response:
    """The file this learner uploaded, as they uploaded it (S61) — archived sources included."""
    source = await session.get(Source, source_id)
    if source is None or source.learner_id != learner.id or not source.blob_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    try:
        data = await blobstore.get(source.blob_key)
    except Exception as exc:  # the bytes are gone from the store
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from exc
    return Response(
        content=data,
        media_type=source.content_type or "application/octet-stream",
        headers={"Content-Disposition": _attachment(source.origin)},
    )


def _attachment(origin: str) -> str:
    """A download header for any filename: an ASCII fallback, and the exact name (RFC 6266)."""
    name = "".join(ch for ch in origin if ch.isprintable())
    fallback = "".join(ch if ch.isascii() and ch not in '"\\' else "_" for ch in name)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.delete("/me", response_model=DeletionRequestRead, status_code=status.HTTP_202_ACCEPTED)
async def request_delete_me(
    session: SessionDep,
    learner: AccountHolder,
    settings: SettingsDep,
    blobstore: BlobStoreDep,
    provider: IdentityProviderDep,
    now: bool = False,
):
    """Delete this account (V12): access ends now; it can be restored for the recovery window
    by signing in again, then it is erased.

    ``now`` erases in the same call, still by way of the request. It cannot be a second
    request: the request revokes every session, the caller's included. A pending account that
    signs in again erases through ``POST /me/deletion/erase``.
    """
    learner_id = learner.id
    pending = await svc.request_deletion(session, learner_id, settings=settings)
    due_at = pending.deletion_due_at
    assert due_at is not None
    if now:
        await svc.erase_learner(session, blobstore, provider, learner_id)
    return DeletionRequestRead(due_at=due_at, erased=now)


@router.get("/me/deletion", response_model=DeletionStatusRead)
async def deletion_status(learner: AccountHolder):
    """Whether this account is pending deletion, and when it will be erased."""
    return DeletionStatusRead(
        pending=learner.deletion_due_at is not None,
        requested_at=learner.deletion_requested_at,
        due_at=learner.deletion_due_at,
    )


def _not_pending() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {"code": "not_pending", "message": "This account is not scheduled for deletion."},
    )


@router.post("/me/deletion/restore", response_model=DeletionStatusRead)
async def restore_me(session: SessionDep, learner: AccountHolder):
    """Keep this account: cancel the pending deletion."""
    try:
        await svc.restore_account(session, learner.id)
    except svc.NotPending as exc:
        raise _not_pending() from exc
    return DeletionStatusRead(pending=False, requested_at=None, due_at=None)


@router.post("/me/deletion/erase", response_model=DeletionReportRead)
async def erase_me(
    session: SessionDep,
    learner: AccountHolder,
    blobstore: BlobStoreDep,
    provider: IdentityProviderDep,
):
    """Erase now, without waiting out the recovery window. Only for a pending account: an
    active one asks for deletion first, so there is one way in to erasing.

    Returns the report rather than 204: a deletion that could not remove every uploaded file
    has to say so; those keys are queued and retried (``pending_erasures``).
    """
    if learner.deletion_due_at is None:
        raise _not_pending()
    report = await svc.erase_learner(session, blobstore, provider, learner.id)
    return DeletionReportRead(
        learner_id=report.learner_id,
        blobs_deleted=report.blobs_deleted,
        blobs_retained=report.blobs_retained,
        blobs_failed=len(report.blobs_failed),
        items_deleted=report.items_deleted,
        complete=report.complete,
    )
