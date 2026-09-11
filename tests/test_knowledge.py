"""Knowledge-graph: service-layer and API tests."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.source import Source, SourceKind, SourceStatus
from app.schemas.knowledge import SubjectCreate
from app.services import knowledge as svc

API = "/api/v1"


# --- service layer ----------------------------------------------------------


async def test_service_create_and_list_subject(db_session: AsyncSession) -> None:
    created = await svc.create_subject(db_session, SubjectCreate(slug="algebra", name="Algebra"))
    assert created.id is not None
    subjects = await svc.list_subjects(db_session)
    assert [s.slug for s in subjects] == ["algebra"]


async def test_list_kcs_for_subject_spans_topics(db_session: AsyncSession) -> None:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add(subject)
    await db_session.flush()
    t1 = Topic(subject_id=subject.id, slug="t1", name="T1")
    t2 = Topic(subject_id=subject.id, slug="t2", name="T2")
    db_session.add_all([t1, t2])
    await db_session.flush()
    kc1 = KC(topic_id=t1.id, slug="a", name="A")
    kc2 = KC(topic_id=t2.id, slug="b", name="B")
    db_session.add_all([kc1, kc2])
    await db_session.flush()

    kcs = await svc.list_kcs_for_subject(db_session, subject.id)
    assert {kc.slug for kc in kcs} == {"a", "b"}


async def test_list_root_kcs_excludes_dependents(db_session: AsyncSession) -> None:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    root = KC(topic_id=topic.id, slug="root", name="Root")
    dependent = KC(topic_id=topic.id, slug="dependent", name="Dependent")
    db_session.add_all([root, dependent])
    await db_session.flush()
    db_session.add(KCEdge(kc_id=dependent.id, prereq_kc_id=root.id))
    await db_session.flush()

    roots = await svc.list_root_kcs(db_session, subject.id)
    assert [kc.slug for kc in roots] == ["root"]


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


# --- create_subject_with_graph (service) ------------------------------------


async def test_create_subject_with_graph_builds_full_hierarchy(db_session: AsyncSession) -> None:
    """Service: create_subject_with_graph creates Subject + Topics + KCs in one transaction."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    topics_data = [
        {
            "name": "Integrals",
            "description": "Integration techniques",
            "kcs": [
                {"name": "u-substitution", "description": "Substitution method"},
                {"name": "Integration by parts", "description": "IBP"},
            ],
        },
        {
            "name": "Derivatives",
            "description": "Differentiation",
            "kcs": [
                {"name": "Power rule", "description": "x^n"},
                {"name": "Chain rule", "description": "Composition"},
            ],
        },
    ]

    _result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Calculus",
        subject_description="Calculus fundamentals",
        topics_data=topics_data,
        source_ids=None,
        learner_id=learner.id,
    )
    subject = _result.subject

    assert subject.id is not None
    assert subject.name == "Calculus"
    assert subject.slug == "calculus"
    assert subject.description == "Calculus fundamentals"

    # Check topics were created
    topics = await svc.list_topics(db_session, subject.id)
    assert len(topics) == 2
    topic_names = {t.name for t in topics}
    assert topic_names == {"Integrals", "Derivatives"}

    # Check KCs were created under each topic
    for topic in topics:
        kcs = await svc.list_kcs(db_session, topic.id)
        if topic.name == "Integrals":
            assert len(kcs) == 2
            kc_names = {kc.name for kc in kcs}
            assert kc_names == {"u-substitution", "Integration by parts"}
        else:
            assert len(kcs) == 2
            kc_names = {kc.name for kc in kcs}
            assert kc_names == {"Power rule", "Chain rule"}


async def test_create_subject_with_graph_dedup_topic_slugs(db_session: AsyncSession) -> None:
    """Service: topics with the same name get distinct slugs (dedup within subject)."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    topics_data = [
        {"name": "Functions", "description": None, "kcs": []},
        {"name": "Functions", "description": None, "kcs": []},  # Dup name
    ]

    _result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Math",
        subject_description=None,
        topics_data=topics_data,
        source_ids=None,
        learner_id=learner.id,
    )
    subject = _result.subject

    topics = await svc.list_topics(db_session, subject.id)
    assert len(topics) == 2
    topic_slugs = {t.slug for t in topics}
    # First gets "functions", second gets "functions_2"
    assert "functions" in topic_slugs
    assert "functions_2" in topic_slugs


async def test_create_subject_with_graph_dedup_subject_slug(db_session: AsyncSession) -> None:
    """Service: subject slug dedup — appends _2, _3, etc. on global collision."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    # Create first subject with slug "algebra"
    first_subject = await svc.create_subject(
        db_session, SubjectCreate(slug="algebra", name="Algebra v1")
    )
    assert first_subject.slug == "algebra"

    # Try to create another with a name that slugifies to "algebra"
    topics_data = [{"name": "Basics", "description": None, "kcs": []}]
    _result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Algebra",  # Will slugify to "algebra" which collides
        subject_description=None,
        topics_data=topics_data,
        source_ids=None,
        learner_id=learner.id,
    )
    second_subject = _result.subject

    assert second_subject.slug == "algebra_2"
    assert second_subject.name == "Algebra"


async def test_create_subject_with_graph_reassigns_sources(db_session: AsyncSession) -> None:
    """Service: source reassignment — sets subject_id on owned sources."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    # Create a source owned by the learner
    source = Source(
        learner_id=learner.id,
        kind="file",
        origin="notes.txt",
        status="done",
    )
    db_session.add(source)
    await db_session.flush()

    topics_data = [{"name": "Basics", "description": None, "kcs": []}]
    _result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Physics",
        subject_description=None,
        topics_data=topics_data,
        source_ids=[source.id],
        learner_id=learner.id,
    )
    subject = _result.subject

    # Refresh the source to see the updated subject_id
    await db_session.refresh(source)
    assert source.subject_id == subject.id


# --- API: POST /subjects/commit -----------------------------------------------


async def test_commit_subject_creates_full_graph(api_client: AsyncClient) -> None:
    """Endpoint: POST /subjects/commit returns 201 with the subject + full graph."""
    payload = {
        "subject_name": "Trigonometry",
        "subject_description": "Trig basics",
        "topics": [
            {
                "name": "Unit Circle",
                "description": "The unit circle",
                "kcs": [
                    {"name": "Sine", "description": "sin(x)"},
                    {"name": "Cosine", "description": "cos(x)"},
                ],
            },
        ],
        "source_ids": None,
    }

    r = await api_client.post(f"{API}/subjects/commit", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "Trigonometry"
    assert data["slug"] == "trigonometry"
    assert data["description"] == "Trig basics"
    assert data["id"] is not None


async def test_commit_subject_duplicate_name_409(api_client: AsyncClient) -> None:
    """Endpoint: posting the same subject_name twice returns 409 on second attempt."""
    payload = {
        "subject_name": "Chemistry",
        "subject_description": None,
        "topics": [{"name": "Basics", "description": None, "kcs": []}],
        "source_ids": None,
    }

    # First one succeeds
    r = await api_client.post(f"{API}/subjects/commit", json=payload)
    assert r.status_code == 201

    # Second one fails (same name)
    r = await api_client.post(f"{API}/subjects/commit", json=payload)
    assert r.status_code == 409


async def test_create_subject_with_graph_reassigns_only_owned_sources(
    db_session: AsyncSession,
) -> None:
    """The source-reassignment guard moves the caller's own sources but never another learner's."""
    owner = Learner(handle=f"owner-{uuid.uuid4().hex[:8]}")
    other = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add_all([owner, other])
    await db_session.flush()

    owned = Source(
        learner_id=owner.id,
        kind=SourceKind.FILE,
        origin="owned.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        subject_id=None,
        meta={},
    )
    foreign = Source(
        learner_id=other.id,
        kind=SourceKind.FILE,
        origin="foreign.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        subject_id=None,
        meta={},
    )
    db_session.add_all([owned, foreign])
    await db_session.flush()

    _result = await svc.create_subject_with_graph(
        db_session,
        subject_name="Physics",
        subject_description=None,
        topics_data=[],
        source_ids=[owned.id, foreign.id],
        learner_id=owner.id,
    )
    subject = _result.subject

    await db_session.refresh(owned)
    await db_session.refresh(foreign)
    assert owned.subject_id == subject.id  # caller's own source reassigned
    assert foreign.subject_id is None  # another learner's source left untouched
