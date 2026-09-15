"""Operational signals: is this instance ready, and is work moving? (S60)

Deliberately thin. Nothing here does per-learner work and nothing here returns anything about
a learner, because all of it is polled.

``/ready`` stays open; everything under ``/ops`` no longer is (P10). The split is not about
how sensitive the numbers feel — it is about who asks. A readiness probe is an orchestrator
with no credential, and one that could fail on authentication would take healthy instances out
of rotation for a reason unrelated to their health. The rest answers questions an operator
asks and a stranger should not get for free: what this deployment spends, how much work is
backed up, and which of its parts is broken right now. See ``require_operator`` for the two
credentials that open them.
"""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import BlobStoreDep, OperatorDep, SessionDep, SettingsDep
from app.core.alerts import AlertReport, evaluate
from app.core.readiness import ReadinessReport, readiness
from app.schemas.ops import AlertTransitionRead
from app.services import alert_history
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


@router.get("/ops/ingestion", response_model=IngestionBacklog, dependencies=[OperatorDep])
async def ingestion_backlog(session: SessionDep, settings: SettingsDep):
    """Queue depth, the age of the oldest waiting source, and lease health.

    The thing to alert on is `oldest_pending_age_seconds`: it rises the moment the queue stops
    draining and keeps rising, where a count can hold steady while nothing is processed at all.
    A `stalled` queue — work waiting, nothing in flight — is the shape of a dead consumer, and
    is what learners experience as an upload that never becomes a lesson.
    """
    return await backlog(session, settings=settings)


@router.get("/ops/spend", response_model=SpendWindow, dependencies=[OperatorDep])
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


@router.get(
    "/ops/alerts/history",
    response_model=list[AlertTransitionRead],
    dependencies=[OperatorDep],
)
async def alert_history_(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    name: Annotated[str | None, Query()] = None,
):
    """When conditions started and stopped firing, newest first (P11).

    The endpoint above answers "is anything wrong now"; this answers "was anything wrong at
    three in the morning", which is the question an operator actually has and which an
    on-demand predicate cannot answer at all. Rows are *transitions*, so the list is as long as
    the number of things that happened rather than the number of times anybody polled.
    """
    return await alert_history.history(session, limit=limit, name=name)


@router.get("/ops/alerts", response_model=AlertReport, dependencies=[OperatorDep])
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
