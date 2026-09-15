"""The administrator's read of this deployment (P10).

Split from ``ops`` on purpose, and the split is about *what is being read* rather than how
sensitive it feels. ``/ops/*`` reports aggregates — spend, queue depth, which conditions are
firing — and admits either an administrator or a monitor holding a shared static token. This
reports people: who registered, what they use, what their use costs. A shared secret sitting in
a monitor's configuration is the wrong credential for that, so these take an administrator's
session and nothing else.

Read-only, deliberately. Everything an administrator might *do* — suspend an account, refund a
budget, impersonate for support — is a separate decision with its own audit requirements, and
P10's impersonation half is not started. A portal that can only look is a portal that cannot yet
be used to do something nobody recorded.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentAdmin, SessionDep, SettingsDep
from app.services.admin import LearnerUsage, learner_usage

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/learners", response_model=list[LearnerUsage])
async def learners(
    _: CurrentAdmin,
    session: SessionDep,
    settings: SettingsDep,
    hours: Annotated[int | None, Query(ge=1, le=24 * 365)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Everyone with an account, most expensive first, with what they did in the window.

    Defaults to the same window as ``/ops/spend`` so the per-learner figures add up to
    something a reader has already seen, rather than to a total from a different fortnight.

    A learner with no calls in the window is still listed, with zeroes. They are the row worth
    reading: somebody registered and did not come back, and leaving them out would make the
    deployment look healthier than it is.
    """
    window_hours = hours if hours is not None else settings.spend_window_hours
    return await learner_usage(session, hours=window_hours, limit=limit)
