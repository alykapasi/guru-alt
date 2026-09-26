"""Export and delete everything held about the current learner (S61).

Retention was implicit in a dozen scattered ``ondelete`` clauses, with no way for a learner to
get their data out or have it removed, and two stores no foreign key reaches. See
``app.services.retention`` for the policy these two endpoints execute.
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    AccountHolder,
    BlobStoreDep,
    CurrentLearner,
    IdentityProviderDep,
    SessionDep,
    SettingsDep,
)
from app.schemas.retention import (
    DeletionReportRead,
    DeletionRequestRead,
    DeletionStatusRead,
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
async def export_me(session: SessionDep, learner: AccountHolder) -> dict:
    """Everything held about this learner, as JSON. Uploads appear as metadata, not bytes."""
    try:
        return await svc.export_learner(session, learner.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "learner not found") from exc


@router.delete("/me", response_model=DeletionRequestRead, status_code=status.HTTP_202_ACCEPTED)
async def request_delete_me(session: SessionDep, learner: AccountHolder, settings: SettingsDep):
    """Delete this account (V12): access ends now; it can be restored for the recovery window
    by signing in again, then it is erased. ``POST /me/deletion/erase`` erases at once."""
    pending = await svc.request_deletion(session, learner.id, settings=settings)
    assert pending.deletion_due_at is not None
    return DeletionRequestRead(due_at=pending.deletion_due_at)


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
