"""What an administrator can read about who is using this deployment (P10).

`llm_calls` has carried `learner_id` since Phase 1 and only two things read it: the per-learner
spend cap (S47) and account deletion. So the deployment could total the bill and could not say
whose it was. The access boundary is `test_admin_access.py`; this is about whether the answer is
true once somebody is allowed to ask.
"""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import LLMCall
from app.models.learner import Learner
from app.services.admin import learner_usage

API = "/api/v1"


def _naive_now() -> datetime:
    """`llm_calls.created_at` is TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _learner(session: AsyncSession, handle: str) -> Learner:
    learner = Learner(handle=f"{handle}-{uuid.uuid4().hex[:6]}", email=f"{handle}@example.com")
    session.add(learner)
    await session.flush()
    return learner


async def _call(
    session: AsyncSession,
    learner: Learner,
    *,
    cost: float | None,
    age_h: float = 0.0,
) -> None:
    session.add(
        LLMCall(
            learner_id=learner.id,
            role="smart",
            provider="test",
            model="m",
            input_tokens=10,
            output_tokens=5,
            cost_usd=cost,
            created_at=_naive_now() - timedelta(hours=age_h),
        )
    )
    await session.flush()


async def test_a_learner_who_never_called_anything_is_still_listed(
    db_session: AsyncSession,
) -> None:
    """The row worth reading. An inner join would report the alpha as healthier than it is by
    omitting exactly the people who registered and did not come back."""
    quiet = await _learner(db_session, "quiet")

    roster = await learner_usage(db_session, hours=24)

    row = next(r for r in roster.learners if r.id == quiet.id)
    assert row.calls == 0
    assert row.cost_usd == 0.0
    assert row.last_call_at is None


async def test_usage_is_attributed_to_the_learner_who_incurred_it(
    db_session: AsyncSession,
) -> None:
    heavy = await _learner(db_session, "heavy")
    light = await _learner(db_session, "light")
    await _call(db_session, heavy, cost=3.0)
    await _call(db_session, heavy, cost=2.0)
    await _call(db_session, light, cost=0.5)

    rows = {r.id: r for r in (await learner_usage(db_session, hours=24)).learners}

    assert rows[heavy.id].calls == 2
    assert rows[heavy.id].cost_usd == 5.0
    assert rows[light.id].calls == 1
    assert rows[light.id].cost_usd == 0.5


async def test_the_most_expensive_learner_comes_first(db_session: AsyncSession) -> None:
    cheap = await _learner(db_session, "cheap")
    dear = await _learner(db_session, "dear")
    await _call(db_session, cheap, cost=1.0)
    await _call(db_session, dear, cost=9.0)

    roster = await learner_usage(db_session, hours=24)

    assert [r.id for r in roster.learners][:2] == [dear.id, cheap.id]


async def test_a_call_outside_the_window_does_not_count_but_the_learner_still_appears(
    db_session: AsyncSession,
) -> None:
    """The window is in the JOIN, not a WHERE. In a WHERE it would drop the learners whose
    rows are all outside it — the inactive ones, who are the point of the list."""
    lapsed = await _learner(db_session, "lapsed")
    await _call(db_session, lapsed, cost=4.0, age_h=48)

    roster = await learner_usage(db_session, hours=24)
    row = next(r for r in roster.learners if r.id == lapsed.id)

    assert row.calls == 0
    assert row.cost_usd == 0.0
    assert row.last_call_at is None


async def test_an_unpriced_call_is_counted_rather_than_summed_as_zero(
    db_session: AsyncSession,
) -> None:
    """Same distinction the deployment total draws (S48): NULL is "no known price", not free."""
    learner = await _learner(db_session, "unpriced")
    await _call(db_session, learner, cost=None)
    await _call(db_session, learner, cost=1.0)

    roster = await learner_usage(db_session, hours=24)
    row = next(r for r in roster.learners if r.id == learner.id)

    assert row.calls == 2
    assert row.unpriced_calls == 1
    assert row.cost_usd == 1.0


async def test_the_last_call_is_when_they_were_last_here(db_session: AsyncSession) -> None:
    learner = await _learner(db_session, "recent")
    await _call(db_session, learner, cost=1.0, age_h=10)
    await _call(db_session, learner, cost=1.0, age_h=2)

    roster = await learner_usage(db_session, hours=24)
    row = next(r for r in roster.learners if r.id == learner.id)

    assert row.last_call_at is not None
    assert (_naive_now() - row.last_call_at) < timedelta(hours=3)


# --- the endpoint ------------------------------------------------------------------------------


async def test_an_administrator_reads_the_roster(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    learner = await _learner(db_session, "listed")
    await _call(db_session, learner, cost=2.0)

    r = await admin_client.get(f"{API}/admin/learners")

    assert r.status_code == 200
    row = next(x for x in r.json()["learners"] if x["id"] == str(learner.id))
    assert row["calls"] == 1 and row["cost_usd"] == 2.0


async def test_a_truncated_roster_says_how_many_there_were(db_session: AsyncSession) -> None:
    """A page showing a hundred of two hundred looks exactly like one showing all of them —
    and the ordering puts the quiet learners last, so the cap removes the rows the list exists
    to surface. A browser journey found this against a database with 188 accounts."""
    for i in range(3):
        await _learner(db_session, f"crowd{i}")

    roster = await learner_usage(db_session, hours=24, limit=2)

    assert len(roster.learners) == 2
    assert roster.total >= 3


async def test_the_roster_states_the_window_it_was_measured_over(
    db_session: AsyncSession,
) -> None:
    assert (await learner_usage(db_session, hours=72)).window_hours == 72


async def test_an_ordinary_learner_may_not_read_the_roster(api_client: AsyncClient) -> None:
    assert (await api_client.get(f"{API}/admin/learners")).status_code == 403


async def test_the_ops_token_does_not_open_the_roster(anon_client: AsyncClient) -> None:
    """The ops token is a shared static secret in a monitor's configuration. It reads
    aggregates; who the learners are is not an aggregate."""
    from app.api.deps import get_app_settings
    from app.core.config import get_settings
    from app.main import app

    settings = get_settings().model_copy(update={"ops_token": "s3cret-ops-token"})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        r = await anon_client.get(
            f"{API}/admin/learners", headers={"X-Ops-Token": "s3cret-ops-token"}
        )
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 401
