"""Per-subject source switches (S26): who may change them, and what an empty change does."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject
from app.models.learner import Learner

API = "/api/v1"


async def _subject(session: AsyncSession, owner: uuid.UUID | None) -> Subject:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Calculus", owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    return subject


async def test_a_new_subject_has_both_switches_off(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, api_learner.id)
    await db_session.commit()

    body = (await api_client.get(f"{API}/subjects/{subject.id}")).json()

    assert body["include_untagged_sources"] is False
    assert body["sources_only"] is False


async def test_the_owner_can_change_either_switch_alone(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, api_learner.id)
    await db_session.commit()

    first = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"sources_only": True}
    )
    second = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"include_untagged_sources": True}
    )

    assert first.status_code == 200
    assert first.json()["sources_only"] is True
    assert first.json()["include_untagged_sources"] is False
    assert second.json()["sources_only"] is True, "a field left out is left unchanged"
    assert second.json()["include_untagged_sources"] is True


async def test_an_empty_change_returns_the_subject_unchanged(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, api_learner.id)
    await db_session.commit()

    response = await api_client.patch(f"{API}/subjects/{subject.id}/source-settings", json={})

    assert response.status_code == 200
    assert response.json()["sources_only"] is False


async def test_a_curated_subject_refuses_the_change(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, None)
    await db_session.commit()

    response = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"sources_only": True}
    )

    assert response.status_code == 403


async def test_a_strangers_subject_is_not_found(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    stranger = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    subject = await _subject(db_session, stranger.id)
    await db_session.commit()

    response = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"sources_only": True}
    )

    assert response.status_code == 404
    await db_session.refresh(subject)
    assert subject.sources_only is False
