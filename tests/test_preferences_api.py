"""The preferences API (S02, V09)."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.profile import ProfileDimension

API = "/api/v1"


async def _subject(session: AsyncSession, owner: Learner | None) -> uuid.UUID:
    tag = uuid.uuid4().hex[:6]
    subject = Subject(
        slug=f"s-{tag}", name=f"S-{tag}", owner_learner_id=owner.id if owner else None
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def test_every_setting_is_listed_with_where_it_came_from(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject_id = await _subject(db_session, api_learner)
    await api_client.put(f"{API}/preferences/pace", json={"value": "brisk"})

    listed = (
        await api_client.get(f"{API}/preferences", params={"subject_id": str(subject_id)})
    ).json()
    by_key = {p["key"]: p for p in listed}
    assert set(by_key) == {"guidance", "explanation_level", "note_format", "hints", "pace"}
    assert by_key["pace"]["value"] == "brisk" and by_key["pace"]["source"] == "global"
    assert by_key["pace"]["global_value"] == "brisk"
    assert by_key["pace"]["options"] == ["auto", "brisk", "standard", "unhurried"]

    r = await api_client.put(
        f"{API}/preferences/pace", json={"value": "unhurried", "subject_id": str(subject_id)}
    )
    assert r.status_code == 200
    assert r.json()["value"] == "unhurried" and r.json()["source"] == "subject"
    assert r.json()["global_value"] == "brisk"


async def test_an_inferred_value_is_shown_beside_the_setting(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    db_session.add(
        ProfileDimension(
            learner_id=api_learner.id,
            key="note_format",
            value="mnemonic",
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()

    listed = (await api_client.get(f"{API}/preferences")).json()
    by_key = {p["key"]: p for p in listed}
    assert by_key["note_format"]["inferred"] == "mnemonic"
    assert by_key["explanation_level"]["inferred"] is None


async def test_bad_keys_values_and_subjects_are_refused(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    stranger = Learner(handle=f"x-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    theirs = await _subject(db_session, stranger)
    curated = await _subject(db_session, None)

    assert (await api_client.put(f"{API}/preferences/font", json={"value": "x"})).status_code == 422
    assert (
        await api_client.put(f"{API}/preferences/pace", json={"value": "warp"})
    ).status_code == 422
    assert (
        await api_client.put(
            f"{API}/preferences/pace", json={"value": "brisk", "subject_id": str(theirs)}
        )
    ).status_code == 404
    assert (
        await api_client.get(f"{API}/preferences", params={"subject_id": str(theirs)})
    ).status_code == 404
    assert (
        await api_client.put(
            f"{API}/preferences/pace", json={"value": "brisk", "subject_id": str(curated)}
        )
    ).status_code == 200
