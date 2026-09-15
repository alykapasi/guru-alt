"""An alert that fires and resolves overnight has to leave a trace (P11).

`app.core.alerts` is a predicate: it says what is wrong *now*. That is the right shape for
something anything can poll and it cannot answer the question an operator actually has the
morning after. A condition could fire, resolve, and leave nobody any the wiser.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alerts import Alert, AlertReport, Severity
from app.models.ops import AlertTransition
from app.services import alert_history

API = "/api/v1"

_CHECKED = ["spend_over_budget", "leases_expired"]


def _report(*firing: Alert) -> AlertReport:
    return AlertReport(firing=list(firing), checked=_CHECKED)


def _alert(name: str = "spend_over_budget", severity: Severity = "critical") -> Alert:
    return Alert(name=name, severity=severity, detail="$41.20 over", action="check the sweep")


async def test_a_condition_that_starts_firing_is_recorded(db_session: AsyncSession) -> None:
    changed = await alert_history.record(db_session, _report(_alert()))

    assert [(c.name, c.firing) for c in changed] == [("spend_over_budget", True)]
    assert changed[0].detail == "$41.20 over"
    assert changed[0].action == "check the sweep", "the alert carries its own evidence forward"


async def test_a_condition_still_firing_writes_nothing_the_second_time(
    db_session: AsyncSession,
) -> None:
    """Transitions, not evaluations. A row a minute would bury the two rows anybody wants, and
    a notification built on this would page somebody sixty times for one incident."""
    await alert_history.record(db_session, _report(_alert()))

    again = await alert_history.record(db_session, _report(_alert()))

    assert again == []
    rows = (await db_session.scalars(select(AlertTransition))).all()
    assert len(rows) == 1


async def test_a_condition_that_stops_firing_is_recorded_as_resolved(
    db_session: AsyncSession,
) -> None:
    await alert_history.record(db_session, _report(_alert()))

    changed = await alert_history.record(db_session, _report())

    assert [(c.name, c.firing) for c in changed] == [("spend_over_budget", False)]
    assert changed[0].severity is None, "a resolution has no severity of its own"


async def test_a_quiet_first_evaluation_writes_nothing(db_session: AsyncSession) -> None:
    """ "Still fine" is not an event. Writing a resolved row for a condition that has never
    fired would fill the table with the absence of news."""
    assert await alert_history.record(db_session, _report()) == []
    assert (await db_session.scalars(select(AlertTransition))).all() == []


async def test_a_condition_that_stopped_being_checked_is_not_treated_as_resolved(
    db_session: AsyncSession,
) -> None:
    """The dangerous case. If the check that found a problem stops running, the problem has not
    gone away — and closing the incident because the evaluation disappeared is exactly how a
    monitoring outage comes to read as good news.
    """
    await alert_history.record(db_session, _report(_alert()))

    # A later report that no longer evaluates this condition at all.
    changed = await alert_history.record(
        db_session, AlertReport(firing=[], checked=["leases_expired"])
    )

    assert changed == []
    assert (await alert_history.current_states(db_session))["spend_over_budget"] is True


async def test_firing_and_resolving_are_separated_in_what_is_returned(
    db_session: AsyncSession,
) -> None:
    """A resolution is worth recording and is not worth waking anybody for, so the caller can
    treat the two differently without re-deriving which is which."""
    await alert_history.record(db_session, _report(_alert("leases_expired", "warning")))

    changed = await alert_history.record(db_session, _report(_alert()))

    assert [(c.name, c.firing) for c in changed] == [
        ("spend_over_budget", True),
        ("leases_expired", False),
    ], "newly firing first"


async def test_the_current_state_is_the_newest_row_per_condition(
    db_session: AsyncSession,
) -> None:
    await alert_history.record(db_session, _report(_alert()))
    await alert_history.record(db_session, _report())
    await alert_history.record(db_session, _report(_alert()))

    assert await alert_history.current_states(db_session) == {"spend_over_budget": True}


async def test_the_newest_row_wins_when_the_clock_and_the_ids_disagree(
    db_session: AsyncSession,
) -> None:
    """The tie that made this table's first implementation wrong, pinned deterministically.

    `created_at` defaults to `now()`, which in Postgres is the *transaction* timestamp — so two
    rows written by one sweep are indistinguishable by it and the order falls through to a
    random UUID. That is not a flaky test, it is a flaky answer: `current_states` would report a
    condition resolved when the last thing that happened was it firing, roughly half the time.

    The ids here are chosen so the older row sorts *after* the newer one, which turns the
    coin-flip into a certainty: anything ordering by the clock returns the wrong row every time.
    Ordering by the sequence is immune, because a sequence is monotonic by construction.
    """
    older, newer = uuid.UUID(int=(1 << 128) - 1), uuid.UUID(int=1)
    db_session.add(AlertTransition(id=older, name="clock_tie", firing=True))
    await db_session.flush()
    db_session.add(AlertTransition(id=newer, name="clock_tie", firing=False))
    await db_session.flush()

    rows = (await db_session.scalars(select(AlertTransition).order_by(AlertTransition.seq))).all()
    assert len({r.created_at for r in rows}) == 1, "one transaction, one clock reading"
    assert [r.firing for r in rows] == [True, False], "the sequence still separates them"

    assert await alert_history.current_states(db_session) == {"clock_tie": False}


async def test_history_comes_back_newest_first_and_can_be_narrowed(
    db_session: AsyncSession,
) -> None:
    """Newest-first has to survive rows written in the same transaction.

    `created_at` defaults to `now()`, which in Postgres is the *transaction* timestamp — so
    every row one sweep writes carries the same value and ordering by it falls through to a
    random UUID. That tie is the same defect behind S56, S14 and S62; here it would let the
    history read backwards and `current_states` report the wrong state. Ordering is by a
    database sequence, which is monotonic by construction and needs no tiebreak.
    """
    await alert_history.record(db_session, _report(_alert()))
    # Both firing in the second report: otherwise the first condition resolves in the same
    # call and the list under test is three rows about two events rather than two about two.
    await alert_history.record(db_session, _report(_alert(), _alert("leases_expired", "warning")))

    everything = await alert_history.history(db_session)
    just_one = await alert_history.history(db_session, name="leases_expired")

    assert [r.name for r in everything][:2] == ["leases_expired", "spend_over_budget"]
    assert {r.name for r in just_one} == {"leases_expired"}
    assert len({r.created_at for r in everything}) == 1, "same transaction clock, different seq"


async def test_the_history_endpoint_serves_what_was_recorded(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await alert_history.record(db_session, _report(_alert()))

    response = await admin_client.get(f"{API}/ops/alerts/history")

    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "spend_over_budget"
    assert body[0]["firing"] is True
    assert body[0]["action"] == "check the sweep"


async def test_the_worker_sweep_evaluates_and_records_without_a_request(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The call site. The predicate existed and nothing ran it, which is the entire gap — so
    the sweep that runs it is the part worth covering, not just the recorder it calls.
    """
    from app.workers import tasks

    recorded: list[AlertReport] = []

    async def fake_record(session, report):
        recorded.append(report)
        return []

    monkeypatch.setattr(tasks.alert_history, "record", fake_record)
    monkeypatch.setattr(tasks, "SessionFactory", lambda: _session_ctx(db_session))

    await tasks._alerts_once()

    assert len(recorded) == 1
    assert recorded[0].checked, "the sweep evaluated the real conditions"


class _session_ctx:
    """Hand the worker the test's session so its sweep runs inside the test transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc: object) -> None:
        return None
