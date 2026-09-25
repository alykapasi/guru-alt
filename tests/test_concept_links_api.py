"""Concept links API: a learner's suggestions and decisions, and the admin review queue (S24)."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.services import concept_links as svc
from tests.test_concept_links import _concept, _kc, _learner, _subject

API = "/api/v1"


async def _endorsed_curated(db_session: AsyncSession):
    concept = await _concept(db_session, "matrices")
    a = await _kc(
        db_session, await _subject(db_session, name="Linear Algebra"), "Matrices", concept
    )
    b = await _kc(db_session, await _subject(db_session, name="Graphics"), "Matrices", concept)
    await svc.sync_candidates(db_session, None)
    (link,) = await svc.curated_queue(db_session)
    admin = await _learner(db_session)
    await svc.set_admin_verdict(db_session, link.id, admin.id, endorse=True, reason="Same object.")
    await db_session.commit()
    return link, a, b


async def test_suggestions_lists_the_endorsed_link(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    link, a, b = await _endorsed_curated(db_session)

    r = await api_client.get(f"{API}/concept-links/suggestions")

    assert r.status_code == 200
    (body,) = r.json()
    assert body["link_id"] == str(link.id)
    assert body["decision"] is None
    assert {body["a"]["subject_name"], body["b"]["subject_name"]} == {"Linear Algebra", "Graphics"}
    assert {a.id, b.id} == {uuid.UUID(body["a"]["kc_id"]), uuid.UUID(body["b"]["kc_id"])}


async def test_deciding_accept_returns_the_updated_suggestion(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    link, _a, _b = await _endorsed_curated(db_session)

    r = await api_client.post(
        f"{API}/concept-links/{link.id}/decision", json={"decision": "accept"}
    )

    assert r.status_code == 200
    assert r.json()["decision"] == "accepted"


async def test_deciding_a_random_link_is_404(api_client: AsyncClient) -> None:
    r = await api_client.post(
        f"{API}/concept-links/{uuid.uuid4()}/decision", json={"decision": "accept"}
    )

    assert r.status_code == 404


async def test_a_non_admin_cannot_reach_the_review_route(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    link, _a, _b = await _endorsed_curated(db_session)

    r = await api_client.post(
        f"{API}/admin/concept-links/{link.id}", json={"endorse": True, "reason": "x"}
    )

    assert r.status_code == 403


async def test_admin_can_list_and_decide_the_curated_queue(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    concept = await _concept(db_session, "sets")
    a = await _kc(db_session, await _subject(db_session, name="A"), "Sets", concept)
    b = await _kc(db_session, await _subject(db_session, name="B"), "Sets", concept)
    await db_session.commit()

    r = await admin_client.get(f"{API}/admin/concept-links")
    assert r.status_code == 200
    (row,) = r.json()
    assert {row["kc_a_name"], row["kc_b_name"]} == {a.name, b.name}
    assert row["verdict"] is None

    r = await admin_client.post(
        f"{API}/admin/concept-links/{row['id']}", json={"endorse": True, "reason": "Same idea."}
    )
    assert r.status_code == 200
    assert r.json()["verdict"] == "endorsed"
