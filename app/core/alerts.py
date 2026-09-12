"""The conditions worth waking somebody for, evaluated in one place (S60).

Readiness, queue depth, lease health and per-call cost were all exposed, and the honest note in
the tracker was that *nothing polls them*. A signal nobody evaluates is documentation, not
monitoring: it moves the work of knowing the thresholds, and of remembering to look, onto a
person who is by definition busy with something else at the moment it matters.

So this is deliberately not a dashboard and not a pager — it is the *predicate*. One endpoint
whose body says which conditions are firing, at what severity, and what to do about each, taken
from the same runbook an operator would otherwise have to read under pressure. Anything that
can poll an HTTP endpoint and alert on a JSON field can alert on this; nothing about the
alerting system has to be decided in order for the thresholds to live in one place and be
tested.

It answers 200 whether or not anything is firing. Readiness is the endpoint that returns 503 —
conflating "this instance should stop taking traffic" with "somebody should look at this" would
take a deployment out of rotation because its bill is high.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.core.config import Settings
from app.core.readiness import ReadinessReport
from app.services.ingestion import IngestionBacklog
from app.services.spend import SpendWindow

Severity = Literal["critical", "warning"]


class Alert(BaseModel):
    """One condition, and what an operator does about it."""

    name: str
    severity: Severity
    # The value that tripped it, rendered — so the alert carries its own evidence and the
    # first thing a responder does is not "go and find out how bad".
    detail: str
    action: str


class AlertReport(BaseModel):
    """Which conditions are firing right now."""

    firing: list[Alert]
    # Every condition that was evaluated and is not firing. Present so a silent report is
    # distinguishable from one where the checks did not run at all.
    checked: list[str]


def evaluate(
    *,
    readiness: ReadinessReport,
    backlog: IngestionBacklog,
    spend: SpendWindow,
    settings: Settings,
) -> AlertReport:
    """Every alert condition, against the thresholds in settings."""
    firing: list[Alert] = []
    checked = [
        "dependencies_unavailable",
        "durable_checkpoints_off",
        "ingestion_stalled",
        "ingestion_backlog_ageing",
        "leases_expired",
        "spend_over_budget",
    ]

    failed = [dep.name for dep in readiness.dependencies if not dep.ok]
    if failed:
        firing.append(
            Alert(
                name="dependencies_unavailable",
                severity="critical",
                detail=f"not answering: {', '.join(failed)}",
                action="This instance is already out of rotation (/api/v1/ready returns 503). "
                "Check the dependency before restarting the app.",
            )
        )

    if not readiness.durable_checkpoints:
        firing.append(
            Alert(
                name="durable_checkpoints_off",
                severity="critical",
                detail="the checkpointer fell back to in-process state",
                action="Paused practice and goal negotiations will not survive a restart, and "
                "two workers can resume the same one. Fix the database connection and restart.",
            )
        )

    if backlog.stalled:
        firing.append(
            Alert(
                name="ingestion_stalled",
                severity="critical",
                detail=f"{backlog.pending} source(s) waiting, none in flight",
                action="The worker is dead or not consuming. Check worker logs and restart it.",
            )
        )
    elif (backlog.oldest_pending_age_seconds or 0.0) > settings.alert_pending_age_seconds:
        firing.append(
            Alert(
                name="ingestion_backlog_ageing",
                severity="warning",
                detail=f"oldest pending source is {backlog.oldest_pending_age_seconds:.0f}s old",
                action="Saturated rather than stuck if processing is at max_concurrent_jobs — "
                "add worker replicas. Otherwise treat as stalled.",
            )
        )

    if backlog.expired_leases >= settings.alert_expired_leases:
        firing.append(
            Alert(
                name="leases_expired",
                severity="warning",
                detail=f"{backlog.expired_leases} claimed source(s) with a lapsed lease",
                action="Persisting past the reconcile interval means the reconciler is not "
                "running. `uv run poe reconcile-ingestion` sweeps now; then find out why.",
            )
        )

    if spend.over_budget:
        firing.append(
            Alert(
                name="spend_over_budget",
                severity="warning",
                detail=f"${spend.cost_usd:.2f} in {spend.window_hours}h against a "
                f"${spend.budget_usd:.2f} budget"
                + (
                    f" (plus {spend.unpriced_calls} unpriced calls)" if spend.unpriced_calls else ""
                ),
                action="Check /api/v1/ops/spend for which role and model. A runaway is usually "
                "one loop, not general growth.",
            )
        )

    return AlertReport(firing=firing, checked=checked)
