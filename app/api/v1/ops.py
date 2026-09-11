"""Operational signals: is this instance ready, and is work moving? (S60)

Deliberately unauthenticated and deliberately thin. These are read by an orchestrator and a
monitor, neither of which holds a learner session, and both of which poll — so nothing here
does per-learner work, and nothing here returns anything about a learner.
"""

from fastapi import APIRouter, Response, status

from app.api.deps import BlobStoreDep, SessionDep, SettingsDep
from app.core.readiness import ReadinessReport, readiness
from app.services.ingestion import IngestionBacklog, backlog

router = APIRouter(tags=["ops"])


@router.get("/ready", response_model=ReadinessReport)
async def ready(session: SessionDep, store: BlobStoreDep, response: Response):
    """Whether this instance should be given traffic.

    503 when a dependency a request needs is not answering, so an orchestrator takes the
    instance out of rotation instead of routing to one that will fail every call. `/health`
    stays separate and stays trivial: a dependency blip must not get a healthy process killed
    and restarted into the same blip.
    """
    report = await readiness(session, store)
    if not report.ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/ops/ingestion", response_model=IngestionBacklog)
async def ingestion_backlog(session: SessionDep, settings: SettingsDep):
    """Queue depth, the age of the oldest waiting source, and lease health.

    The thing to alert on is `oldest_pending_age_seconds`: it rises the moment the queue stops
    draining and keeps rising, where a count can hold steady while nothing is processed at all.
    A `stalled` queue — work waiting, nothing in flight — is the shape of a dead consumer, and
    is what learners experience as an upload that never becomes a lesson.
    """
    return await backlog(session, settings=settings)
