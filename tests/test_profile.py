"""Learner profile: service + HTTP level (plumbing — dimension catalog tests live elsewhere)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.profile import LearnerProfile, ProfileDimension
from app.services import profile as svc

API = "/api/v1"


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


# --- service layer ------------------------------------------------------


async def test_refresh_profile_over_empty_catalog_is_a_noop(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    dims = await svc.refresh_profile(db_session, learner.id, fake_llm_client())
    assert dims == []
    profile = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner.id)
    )
    assert profile is not None  # header row created even with nothing to show yet


async def test_get_snapshot_is_read_only(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="pace",
            value={"median_seconds": 12.0},
            uncertainty=0.5,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.commit()
    snapshot = await svc.get_snapshot(db_session, learner.id)
    assert [d.key for d in snapshot] == ["pace"]


async def test_reset_dimension_unknown_key_raises(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    with pytest.raises(KeyError):
        await svc.reset_dimension(db_session, learner.id, "not_a_real_dimension")


# --- HTTP level -----------------------------------------------------------


async def test_profile_endpoints_round_trip(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    r = await api_client.get(f"{API}/profile")
    assert r.status_code == 200
    assert r.json() == {"dimensions": []}

    r = await api_client.post(f"{API}/profile/refresh")
    assert r.status_code == 200
    assert r.json() == {"dimensions": []}

    r = await api_client.post(f"{API}/profile/not_a_real_dimension/reset")
    assert r.status_code == 404
