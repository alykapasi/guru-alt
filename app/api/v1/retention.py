"""Export and delete everything held about the current learner (S61).

Retention was implicit in a dozen scattered ``ondelete`` clauses, with no way for a learner to
get their data out or have it removed, and two stores no foreign key reaches. See
``app.services.retention`` for the policy these two endpoints execute.
"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import BlobStoreDep, CurrentLearner, SessionDep
from app.schemas.retention import DeletionReportRead, RetentionPolicyRead, StoreRetentionRead
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
async def export_me(session: SessionDep, learner: CurrentLearner) -> dict:
    """Everything held about this learner, as JSON. Uploads appear as metadata, not bytes."""
    try:
        return await svc.export_learner(session, learner.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "learner not found") from exc


@router.delete("/me", response_model=DeletionReportRead)
async def delete_me(session: SessionDep, learner: CurrentLearner, blobstore: BlobStoreDep):
    """Erase this learner from every store.

    Returns the report rather than 204: a deletion that could not remove every uploaded file
    has to say so, because those keys can no longer be found by walking the database.
    """
    report = await svc.delete_learner(session, blobstore, learner.id)
    return DeletionReportRead(
        learner_id=report.learner_id,
        blobs_deleted=report.blobs_deleted,
        blobs_retained=report.blobs_retained,
        blobs_failed=len(report.blobs_failed),
        items_deleted=report.items_deleted,
        complete=report.complete,
    )
