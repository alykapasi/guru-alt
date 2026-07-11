"""Lesson plan: service + HTTP level (dynamic-revision/orchestration layer)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.models.profile import ProfileDimension
from app.services import lesson_plan as svc

API = "/api/v1"


async def _graph(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    """A subject with a root KC and a dependent KC (root -> dependent prerequisite)."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    root = KC(topic_id=topic.id, slug="a-root", name="A Root")
    dependent = KC(topic_id=topic.id, slug="b-dependent", name="B Dependent")
    session.add_all([root, dependent])
    await session.flush()
    session.add(KCEdge(kc_id=dependent.id, prereq_kc_id=root.id))
    await session.flush()
    return learner, subject, root, dependent


async def _mastered_state(session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID) -> None:
    session.add(LearnerKCState(learner_id=learner_id, kc_id=kc_id, ability=1.5, uncertainty=0.3))
    await session.flush()


async def _due_review_state(session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID) -> None:
    session.add(
        LearnerKCState(
            learner_id=learner_id,
            kc_id=kc_id,
            ability=0.8,
            uncertainty=0.4,
            due_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.flush()


# --- generate_lesson_plan ---------------------------------------------------


async def test_generate_lesson_plan_no_goal_targets_whole_subject_in_topo_order(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, dependent = await _graph(db_session)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    assert [s["kc_id"] for s in plan.steps] == [str(root.id), str(dependent.id)]
    assert plan.steps[0]["status"] == "active"
    assert plan.steps[1]["status"] == "pending"


async def test_generate_lesson_plan_with_goal_selects_objective_and_pulls_in_prereqs(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, dependent = await _graph(db_session)
    # Candidates ordered (topic.slug, kc.slug): [a-root=1, b-dependent=2].
    reply = json.dumps({"kcs": [2]})
    plan = await svc.generate_lesson_plan(
        db_session,
        fake_llm_client(reply),
        learner_id=learner.id,
        subject_id=subject.id,
        goal="teach me B",
    )
    # The dependent's prerequisite (root) is pulled in even though only "dependent" was targeted.
    assert {s["kc_id"] for s in plan.steps} == {str(root.id), str(dependent.id)}


async def test_generate_lesson_plan_marks_mastered_kc_done_and_advances_active(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, dependent = await _graph(db_session)
    await _mastered_state(db_session, learner.id, root.id)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    by_kc = {s["kc_id"]: s for s in plan.steps}
    assert by_kc[str(root.id)]["status"] == "done"
    assert by_kc[str(dependent.id)]["status"] == "active"


async def test_generate_lesson_plan_surfaces_due_review_ahead_of_new_steps(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    await _due_review_state(db_session, learner.id, root.id)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    assert plan.steps[0]["kc_id"] == str(root.id)
    assert plan.steps[0]["step_type"] == "review"
    assert plan.steps[0]["status"] == "active"


async def test_generate_lesson_plan_applies_profile_scaffolding(db_session: AsyncSession) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="optimal_challenge",
            value=0.65,
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="pace",
            value={"median_seconds": 10.0, "trend": "speeding_up"},
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()

    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    assert plan.pacing == "brisk"
    assert plan.steps[0]["target_difficulty"] == 0.65


async def test_generate_lesson_plan_regenerating_replaces_in_place(
    db_session: AsyncSession,
) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    first = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    second = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal="hi"
    )
    assert first.id == second.id
    assert second.goal == "hi"


# --- revise_plan / get_lesson_plan / get_active_step_context -----------------


async def test_revise_plan_is_a_noop_when_no_plan_exists(db_session: AsyncSession) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    result = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert result is None
    assert await svc.get_lesson_plan(db_session, learner.id, subject.id) is None


async def test_revise_plan_picks_up_new_mastery_without_regenerating(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, dependent = await _graph(db_session)
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    await _mastered_state(db_session, learner.id, root.id)

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    by_kc = {s["kc_id"]: s for s in revised.steps}
    assert by_kc[str(root.id)]["status"] == "done"
    assert by_kc[str(dependent.id)]["status"] == "active"


async def test_get_lesson_plan_is_read_only(db_session: AsyncSession) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    assert await svc.get_lesson_plan(db_session, learner.id, subject.id) is None
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    plan = await svc.get_lesson_plan(db_session, learner.id, subject.id)
    assert plan is not None


async def test_get_active_step_context_none_when_no_plan(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    assert await svc.get_active_step_context(db_session, learner.id) is None


async def test_get_active_step_context_reflects_the_active_step(db_session: AsyncSession) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="optimal_challenge",
            value=0.5,
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    context = await svc.get_active_step_context(db_session, learner.id)
    assert context is not None
    assert context.subject_name == subject.name
    assert context.kc_name == root.name
    assert context.target_difficulty == 0.5


# --- HTTP level -----------------------------------------------------------
# No goal is submitted below, so generate_lesson_plan never calls the objective-selection
# LLM (see `if goal and kcs` in app/services/lesson_plan.py) — no fake-LLM override needed.


async def test_lesson_plan_endpoints_round_trip(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    r = await api_client.post(f"{API}/subjects", json={"slug": "phys", "name": "Physics"})
    subject_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": "t", "name": "T"}
    )
    topic_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": "a-root", "name": "A Root"}
    )
    root_id = r.json()["id"]

    r = await api_client.get(f"{API}/subjects/{subject_id}/lesson-plan")
    assert r.status_code == 404

    r = await api_client.post(f"{API}/subjects/{subject_id}/lesson-plan", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["kc_id"] for s in body["steps"]] == [root_id]
    assert body["steps"][0]["status"] == "active"

    r = await api_client.get(f"{API}/subjects/{subject_id}/lesson-plan")
    assert r.status_code == 200
    assert r.json()["id"] == body["id"]

    r = await api_client.post(f"{API}/subjects/{uuid.uuid4()}/lesson-plan", json={})
    assert r.status_code == 404
    r = await api_client.get(f"{API}/subjects/{uuid.uuid4()}/lesson-plan")
    assert r.status_code == 404
