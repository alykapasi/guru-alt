"""Spend, alerts, and whether the bytes are still there (S60).

The tracker's honest note was that these signals existed and *nothing polled them*. These
cover the three that are now evaluated rather than merely exposed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alerts import evaluate
from app.core.config import Settings
from app.core.readiness import DependencyStatus, ReadinessReport
from app.models.chat import LLMCall
from app.models.learner import Learner
from app.models.source import Source, SourceKind, SourceStatus
from app.services import blob_integrity
from app.services.ingestion import IngestionBacklog
from app.services.spend import SpendWindow, window
from app.storage.memory import InMemoryBlobStore

API = "/api/v1"


def _naive_now() -> datetime:
    """`llm_calls.created_at` is TIMESTAMP WITHOUT TIME ZONE."""
    return datetime.now(UTC).replace(tzinfo=None)


async def _call(session: AsyncSession, *, cost: float | None, role="smart", model="m", age_h=0.0):
    session.add(
        LLMCall(
            role=role,
            provider="test",
            model=model,
            input_tokens=10,
            output_tokens=5,
            cost_usd=cost,
            created_at=_naive_now() - timedelta(hours=age_h),
        )
    )
    await session.flush()


# --- spend ------------------------------------------------------------------------------------


async def test_spend_totals_the_window_and_splits_it_by_role_and_model(
    db_session: AsyncSession,
) -> None:
    await _call(db_session, cost=1.0, role="smart", model="sonnet")
    await _call(db_session, cost=0.25, role="fast", model="haiku")
    await _call(db_session, cost=0.25, role="fast", model="haiku")

    report = await window(db_session, settings=Settings(), hours=24)
    assert report.calls == 3
    assert report.cost_usd == pytest.approx(1.5)
    assert {b.name: b.cost_usd for b in report.by_role} == {
        "smart": pytest.approx(1.0),
        "fast": pytest.approx(0.5),
    }
    assert {b.name for b in report.by_model} == {"sonnet", "haiku"}


async def test_calls_outside_the_window_are_not_counted(db_session: AsyncSession) -> None:
    await _call(db_session, cost=5.0, age_h=48)
    await _call(db_session, cost=1.0, age_h=1)

    report = await window(db_session, settings=Settings(), hours=24)
    assert report.calls == 1
    assert report.cost_usd == pytest.approx(1.0)


async def test_an_unpriced_call_is_reported_rather_than_summed_as_zero(
    db_session: AsyncSession,
) -> None:
    """NULL means "no known price"; 0.0 means "ran locally and cost nothing" (S48).

    Collapsing the two would report a deployment running entirely on unpriced models as
    spending nothing, which is the most expensive way to be wrong.
    """
    await _call(db_session, cost=None, model="unpriced")
    await _call(db_session, cost=0.0, model="ollama")
    await _call(db_session, cost=2.0, model="sonnet")

    report = await window(db_session, settings=Settings(), hours=24)
    assert report.unpriced_calls == 1
    assert report.cost_usd == pytest.approx(2.0)
    unpriced = next(b for b in report.by_model if b.name == "unpriced")
    assert unpriced.unpriced_calls == 1


async def test_no_budget_means_never_over_budget(db_session: AsyncSession) -> None:
    await _call(db_session, cost=1000.0)
    report = await window(db_session, settings=Settings(spend_budget_usd=None), hours=24)
    assert report.budget_usd is None
    assert report.over_budget is False


async def test_spend_past_the_budget_says_so(db_session: AsyncSession) -> None:
    await _call(db_session, cost=12.0)
    report = await window(db_session, settings=Settings(spend_budget_usd=10.0), hours=24)
    assert report.over_budget is True


async def test_the_spend_endpoint_is_open_to_a_monitor(anon_client: AsyncClient) -> None:
    """Read by a poller that holds no learner session, like the other ops endpoints."""
    r = await anon_client.get(f"{API}/ops/spend")
    assert r.status_code == 200
    assert "cost_usd" in r.json()


# --- alerts -----------------------------------------------------------------------------------


def _ready(*, ok=True, durable=True) -> ReadinessReport:
    return ReadinessReport(
        ready=ok,
        durable_checkpoints=durable,
        dependencies=[
            DependencyStatus(name="database", ok=ok, latency_ms=1.0),
            DependencyStatus(name="object_store", ok=True, latency_ms=1.0),
        ],
    )


def _backlog(
    *,
    pending: int = 0,
    processing: int = 0,
    failed: int = 0,
    oldest_pending_age_seconds: float | None = None,
    expired_leases: int = 0,
    max_concurrent_jobs: int = 2,
) -> IngestionBacklog:
    return IngestionBacklog(
        pending=pending,
        processing=processing,
        failed=failed,
        oldest_pending_age_seconds=oldest_pending_age_seconds,
        expired_leases=expired_leases,
        max_concurrent_jobs=max_concurrent_jobs,
    )


def _spend(
    *,
    cost_usd: float = 0.0,
    unpriced_calls: int = 0,
    budget_usd: float | None = None,
    over_budget: bool = False,
) -> SpendWindow:
    return SpendWindow(
        window_hours=24,
        since=datetime.now(UTC),
        calls=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=cost_usd,
        unpriced_calls=unpriced_calls,
        budget_usd=budget_usd,
        over_budget=over_budget,
        by_role=[],
        by_model=[],
    )


def test_a_healthy_deployment_fires_nothing_and_still_says_what_it_checked() -> None:
    """A silent report has to be distinguishable from checks that never ran."""
    report = evaluate(readiness=_ready(), backlog=_backlog(), spend=_spend(), settings=Settings())
    assert report.firing == []
    assert len(report.checked) == 6


def test_a_dead_dependency_is_critical_and_names_it() -> None:
    report = evaluate(
        readiness=_ready(ok=False), backlog=_backlog(), spend=_spend(), settings=Settings()
    )
    alert = next(a for a in report.firing if a.name == "dependencies_unavailable")
    assert alert.severity == "critical"
    assert "database" in alert.detail


def test_losing_durable_checkpoints_is_critical() -> None:
    """Two workers can then resume the same paused practice and grade one answer twice (S17)."""
    report = evaluate(
        readiness=_ready(durable=False), backlog=_backlog(), spend=_spend(), settings=Settings()
    )
    assert any(a.name == "durable_checkpoints_off" for a in report.firing)


def test_work_waiting_with_nothing_in_flight_is_a_dead_consumer() -> None:
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(pending=4, processing=0, oldest_pending_age_seconds=30.0),
        spend=_spend(),
        settings=Settings(),
    )
    names = [a.name for a in report.firing]
    assert "ingestion_stalled" in names
    # Stalled supersedes ageing: one condition, one alert, so a dead worker does not arrive as
    # two pages saying the same thing.
    assert "ingestion_backlog_ageing" not in names


def test_a_saturated_queue_ages_rather_than_stalls() -> None:
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(pending=4, processing=2, oldest_pending_age_seconds=1800.0),
        spend=_spend(),
        settings=Settings(alert_pending_age_seconds=900.0),
    )
    names = [a.name for a in report.firing]
    assert "ingestion_backlog_ageing" in names
    assert "ingestion_stalled" not in names


def test_a_young_backlog_does_not_fire() -> None:
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(pending=1, processing=1, oldest_pending_age_seconds=5.0),
        spend=_spend(),
        settings=Settings(alert_pending_age_seconds=900.0),
    )
    assert report.firing == []


def test_expired_leases_point_at_the_reconciler() -> None:
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(processing=1, expired_leases=3),
        spend=_spend(),
        settings=Settings(),
    )
    alert = next(a for a in report.firing if a.name == "leases_expired")
    assert "reconcile-ingestion" in alert.action


def test_going_over_budget_fires_with_the_figure_in_it() -> None:
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(),
        spend=_spend(cost_usd=42.5, budget_usd=20.0, over_budget=True),
        settings=Settings(),
    )
    alert = next(a for a in report.firing if a.name == "spend_over_budget")
    assert "42.50" in alert.detail and "20.00" in alert.detail


def test_an_over_budget_alert_says_when_the_figure_is_a_floor() -> None:
    report = evaluate(
        readiness=_ready(),
        backlog=_backlog(),
        spend=_spend(cost_usd=42.5, budget_usd=20.0, over_budget=True, unpriced_calls=7),
        settings=Settings(),
    )
    alert = next(a for a in report.firing if a.name == "spend_over_budget")
    assert "7 unpriced" in alert.detail


async def test_the_alerts_endpoint_answers_200_even_while_firing(
    anon_client: AsyncClient,
) -> None:
    """Readiness is the endpoint that 503s. Taking an instance out of rotation because its
    bill is high would be the wrong response to the right signal."""
    r = await anon_client.get(f"{API}/ops/alerts")
    assert r.status_code == 200
    assert "firing" in r.json() and "checked" in r.json()


# --- the bytes behind the rows -----------------------------------------------------------------


async def _source(session: AsyncSession, learner: Learner, key: str | None) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="lecture.pdf",
        status=SourceStatus.DONE,
        content_type="application/pdf",
        blob_key=key,
        meta={},
    )
    session.add(source)
    await session.flush()
    return source


async def test_an_intact_store_reports_every_key_checked(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    for name in ("a", "b"):
        key = f"blobs/{name}"
        await store.put(key, b"bytes")
        await _source(db_session, api_learner, key)

    report = await blob_integrity.check(db_session, store)
    assert report.checked == 2
    assert report.intact


async def test_a_restored_database_with_an_empty_bucket_is_caught(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The failure this exists for: every row present, every byte gone, and nothing to
    re-ingest from because the blobs were content-addressed and de-duplicated (S77)."""
    store = InMemoryBlobStore()
    present = "blobs/present"
    await store.put(present, b"bytes")
    await _source(db_session, api_learner, present)
    gone = await _source(db_session, api_learner, "blobs/gone")

    report = await blob_integrity.check(db_session, store)
    assert report.checked == 2
    assert not report.intact
    assert [m.source_id for m in report.missing] == [gone.id]
    assert report.missing[0].origin == "lecture.pdf"


async def test_a_source_with_no_stored_object_is_not_a_missing_one(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """A fetched URL has no blob by design; reporting those would bury the real failures."""
    await _source(db_session, api_learner, None)
    report = await blob_integrity.check(db_session, InMemoryBlobStore())
    assert report.checked == 0
    assert report.intact


async def test_the_in_memory_store_answers_exists_for_a_key_it_holds() -> None:
    store = InMemoryBlobStore()
    assert await store.exists("nothing/here") is False
    await store.put("something/here", b"x")
    assert await store.exists("something/here") is True
