"""Knowledge-graph: service-layer and API tests."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.knowledge import SubjectCreate
from app.services import knowledge as svc

API = "/api/v1"


# --- service layer ----------------------------------------------------------


async def test_service_create_and_list_subject(db_session: AsyncSession) -> None:
    created = await svc.create_subject(db_session, SubjectCreate(slug="algebra", name="Algebra"))
    assert created.id is not None
    subjects = await svc.list_subjects(db_session)
    assert [s.slug for s in subjects] == ["algebra"]


# --- API: happy path through the whole graph --------------------------------


async def test_build_graph_with_prerequisite(api_client: AsyncClient) -> None:
    # Subject
    r = await api_client.post(f"{API}/subjects", json={"slug": "calculus", "name": "Calculus"})
    assert r.status_code == 201, r.text
    subject_id = r.json()["id"]

    # Topic
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": "integrals", "name": "Integrals"}
    )
    assert r.status_code == 201
    topic_id = r.json()["id"]

    # Two KCs
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": "u-substitution", "name": "u-substitution"}
    )
    assert r.status_code == 201
    usub_id = r.json()["id"]

    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs",
        json={"slug": "integration-by-parts", "name": "Integration by parts"},
    )
    assert r.status_code == 201
    ibp_id = r.json()["id"]

    # u-substitution is a prerequisite of integration-by-parts
    r = await api_client.post(
        f"{API}/kcs/{ibp_id}/prerequisites", json={"prereq_kc_id": usub_id, "weight": 0.8}
    )
    assert r.status_code == 201

    # Read it back as a detail with prerequisites
    r = await api_client.get(f"{API}/kcs/{ibp_id}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["slug"] == "integration-by-parts"
    assert len(detail["prerequisites"]) == 1
    assert detail["prerequisites"][0]["prereq_kc_id"] == usub_id

    # And it shows up in the topic listing
    r = await api_client.get(f"{API}/topics/{topic_id}/kcs")
    assert {kc["slug"] for kc in r.json()} == {"u-substitution", "integration-by-parts"}


# --- API: error cases -------------------------------------------------------


async def test_get_missing_subject_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/subjects/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_topic_under_missing_subject_404(api_client: AsyncClient) -> None:
    r = await api_client.post(
        f"{API}/subjects/{uuid.uuid4()}/topics", json={"slug": "x", "name": "X"}
    )
    assert r.status_code == 404


async def test_duplicate_subject_slug_409(api_client: AsyncClient) -> None:
    body = {"slug": "physics", "name": "Physics"}
    assert (await api_client.post(f"{API}/subjects", json=body)).status_code == 201
    assert (await api_client.post(f"{API}/subjects", json=body)).status_code == 409


async def test_self_prerequisite_400(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/subjects", json={"slug": "bio", "name": "Bio"})
    subject_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": "cells", "name": "Cells"}
    )
    topic_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": "mitosis", "name": "Mitosis"}
    )
    kc_id = r.json()["id"]

    r = await api_client.post(f"{API}/kcs/{kc_id}/prerequisites", json={"prereq_kc_id": kc_id})
    assert r.status_code == 400


async def test_invalid_slug_422(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/subjects", json={"slug": "Not A Slug", "name": "x"})
    assert r.status_code == 422
