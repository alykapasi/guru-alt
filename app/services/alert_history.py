"""Remember when an alert started and stopped firing (S60/P11).

``app.core.alerts`` is a predicate: it says what is wrong *now*. That is the right shape for a
thing anything can poll, and it cannot answer the question an operator actually asks the morning
after — was anything wrong overnight? A condition could fire, resolve, and leave no trace.

This records the **changes**. Recording every evaluation would write a row a minute forever and
bury the two rows anybody wants; recording only transitions means the table's length is the
number of things that actually happened, and "is X firing?" is the `firing` flag on X's most
recent row.

**A transition is also the only honest thing to notify on.** Delivering the firing set on every
poll would page somebody every minute for one incident. What changed is what is news, which is
why this returns the transitions it wrote — the caller logs them, and anything watching the log
gets one line per event rather than one per poll.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alerts import Alert, AlertReport
from app.models.ops import AlertTransition


async def current_states(session: AsyncSession) -> dict[str, bool]:
    """The latest known state of every condition that has ever changed state.

    A condition absent from this map has never fired, which is why a first evaluation that
    finds nothing wrong writes nothing: "still fine" is not an event.
    """
    newest = select(
        AlertTransition.name,
        AlertTransition.firing,
        func.row_number()
        .over(
            partition_by=AlertTransition.name,
            order_by=AlertTransition.seq.desc(),
        )
        .label("rank"),
    ).subquery()
    rows = await session.execute(select(newest.c.name, newest.c.firing).where(newest.c.rank == 1))
    return {name: firing for name, firing in rows.all()}


async def record(session: AsyncSession, report: AlertReport) -> list[AlertTransition]:
    """Write a row for every condition whose state differs from its last recorded one.

    Returns what changed, newly-firing first — a resolution is worth recording and is not worth
    waking anybody for, so the caller can treat the two differently without re-deriving which
    is which.

    Only conditions the report actually *checked* are considered. A condition missing from the
    report was not evaluated, and treating "not evaluated" as "resolved" would close an incident
    because the check that found it stopped running.
    """
    previous = await current_states(session)
    firing_now = {alert.name: alert for alert in report.firing}
    evaluated = set(report.checked) | set(firing_now)

    started: list[AlertTransition] = []
    resolved: list[AlertTransition] = []
    for name in sorted(evaluated):
        alert: Alert | None = firing_now.get(name)
        is_firing = alert is not None
        if previous.get(name, False) == is_firing:
            continue
        row = AlertTransition(
            name=name,
            firing=is_firing,
            severity=alert.severity if alert else None,
            detail=alert.detail if alert else None,
            action=alert.action if alert else None,
        )
        session.add(row)
        (started if is_firing else resolved).append(row)

    changed = started + resolved
    if changed:
        await session.commit()
        for row in changed:
            await session.refresh(row)
    return changed


async def history(
    session: AsyncSession, *, limit: int = 100, name: str | None = None
) -> Sequence[AlertTransition]:
    """Recent transitions, newest first, optionally for one condition."""
    stmt = (
        select(AlertTransition).order_by(AlertTransition.seq.desc()).limit(max(1, min(limit, 1000)))
    )
    if name is not None:
        stmt = stmt.where(AlertTransition.name == name)
    return (await session.scalars(stmt)).all()
