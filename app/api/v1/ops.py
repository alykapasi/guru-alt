"""Operational signals: is this instance ready, and is work moving? (S60)

Deliberately unauthenticated and deliberately thin. These are read by an orchestrator and a
monitor, neither of which holds a learner session, and both of which poll — so nothing here
does per-learner work, and nothing here returns anything about a learner.
"""

from fastapi import APIRouter, Query, Response, status

from app.api.deps import BlobStoreDep, SessionDep, SettingsDep
from app.core.alerts import AlertReport, evaluate
from app.core.readiness import ReadinessReport, readiness
from app.services.ingestion import IngestionBacklog, backlog
from app.services.spend import SpendWindow
from app.services.spend import window as spend_window

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


@router.get("/ops/spend", response_model=SpendWindow)
async def spend(
    session: SessionDep,
    settings: SettingsDep,
    hours: int | None = Query(default=None, ge=1, le=24 * 90),
):
    """Model spend over a window, by role and by model (S60).

    Cost has been recorded per call since Phase 1 and capped per learner since S47; nothing
    watched the total, so the first signal was the bill. `cost_usd` is a **floor** whenever
    `unpriced_calls` is non-zero — a NULL price means the model has no known one, which is
    deliberately distinct from a local model that genuinely cost nothing.
    """
    return await spend_window(session, settings=settings, hours=hours)


@router.get("/ops/alerts", response_model=AlertReport)
async def alerts(session: SessionDep, store: BlobStoreDep, settings: SettingsDep):
    """Which conditions are worth acting on right now, and what to do about each (S60).

    The signals existed and nothing evaluated them, which put the thresholds in a runbook and
    the remembering in a person. This is the predicate: anything that can poll HTTP and read a
    JSON field can alert on it. It answers 200 whether or not anything is firing — readiness is
    the endpoint that 503s, and taking an instance out of rotation because its bill is high
    would be the wrong response to the right signal.
    """
    return evaluate(
        readiness=await readiness(session, store),
        backlog=await backlog(session, settings=settings),
        spend=await spend_window(session, settings=settings),
        settings=settings,
    )
