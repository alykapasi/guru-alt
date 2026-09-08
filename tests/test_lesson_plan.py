"""Lesson plan: service + HTTP level (dynamic-revision/orchestration layer)."""

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.registry import fake_llm_client
from app.models.assessment import Item, ItemKC, ItemType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.profile import ProfileDimension
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as assessment_svc
from app.services import lesson_plan as svc
from app.services import profile as profile_svc

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
    assert context.kc_id == root.id
    assert context.kc_name == root.name
    assert context.step_type == "new"
    assert context.target_difficulty == 0.5


async def test_get_active_step_context_with_subject_id_is_an_exact_lookup(
    db_session: AsyncSession,
) -> None:
    """A plan for subject B wins over a *more recently updated* plan for subject A, when the
    caller asks for subject B by id — proving exact scoping overrides the heuristic rather
    than just coexisting with it."""
    learner, subject_a, _root_a, _dependent_a = await _graph(db_session)
    subject_b = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    db_session.add(subject_b)
    await db_session.flush()
    topic_b = Topic(subject_id=subject_b.id, slug="t", name="T")
    db_session.add(topic_b)
    await db_session.flush()
    root_b = KC(topic_id=topic_b.id, slug="root-b", name="Root B")
    db_session.add(root_b)
    await db_session.flush()

    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject_b.id, goal=None
    )
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject_a.id, goal=None
    )
    # subject_a's plan is now the most-recently-updated one overall.

    context = await svc.get_active_step_context(db_session, learner.id, subject_id=subject_b.id)
    assert context is not None
    assert context.subject_name == subject_b.name
    assert context.kc_id == root_b.id


async def test_get_active_step_context_with_subject_id_does_not_fall_back(
    db_session: AsyncSession,
) -> None:
    """Subject B has no plan yet, even though the learner has one for subject A — must return
    None, never A's plan."""
    learner, subject_a, _root_a, _dependent_a = await _graph(db_session)
    subject_b = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    db_session.add(subject_b)
    await db_session.flush()

    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject_a.id, goal=None
    )

    assert (
        await svc.get_active_step_context(db_session, learner.id, subject_id=subject_b.id) is None
    )


# --- auto-revision wiring (answer_item / refresh_profile) --------------------


async def _mcq_item(session: AsyncSession, kc_id: uuid.UUID) -> Item:
    item = Item(
        item_type=ItemType.MCQ,
        stem="Q",
        answer_key={"choices": ["a", "b"], "correct": 0},
        kc_links=[ItemKC(kc_id=kc_id, weight=1.0)],
    )
    session.add(item)
    await session.flush()
    return item


async def test_answer_item_auto_revises_a_due_review_step_to_done(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    await _due_review_state(db_session, learner.id, root.id)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    assert plan.steps[0]["step_type"] == "review"
    assert plan.steps[0]["status"] == "active"

    item = await _mcq_item(db_session, root.id)
    await assessment_svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice": 0}), llm=fake_llm_client()
    )

    # Answering advances FSRS's due_at into the future, so the review is no longer due —
    # revise_plan ran automatically inside answer_item and marked it done, with no explicit
    # call to svc.revise_plan or the lesson-plan endpoint. (root also keeps a separate "new"
    # step — not yet past the mastery threshold — since being due for review and not yet
    # mastered are independent facts about a KC.)
    revised = await svc.get_lesson_plan(db_session, learner.id, subject.id)
    assert revised is not None
    review_step = next(
        s for s in revised.steps if s["kc_id"] == str(root.id) and s["step_type"] == "review"
    )
    assert review_step["status"] == "done"


async def test_answer_item_does_not_create_a_plan_that_never_existed(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    item = await _mcq_item(db_session, root.id)
    await assessment_svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice": 0}), llm=fake_llm_client()
    )
    assert await svc.get_lesson_plan(db_session, learner.id, subject.id) is None


async def test_refresh_profile_auto_revises_existing_plan_hints(db_session: AsyncSession) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    assert plan.steps[0]["target_difficulty"] is None

    # Enough graded evidence for optimal_challenge (>= OPTIMAL_CHALLENGE_MIN_EVENTS=3, all in
    # the 0.4-0.8 "productive struggle" band) to compute a concrete value.
    for _ in range(3):
        db_session.add(
            LearningEvent(
                learner_id=learner.id,
                event_type="observation",
                payload={"score": 0.6, "difficulty": 0.75, "item_id": None, "response": None},
            )
        )
    await db_session.flush()

    await profile_svc.refresh_profile(db_session, learner.id, fake_llm_client())

    revised = await svc.get_lesson_plan(db_session, learner.id, subject.id)
    assert revised is not None
    assert revised.steps[0]["target_difficulty"] == 0.75


async def test_refresh_profile_does_not_create_a_plan_that_never_existed(
    db_session: AsyncSession,
) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    await profile_svc.refresh_profile(db_session, learner.id, fake_llm_client())
    assert await svc.get_lesson_plan(db_session, learner.id, subject.id) is None


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


# --- a failed revision must not lose a committed grade (S35) ------------------


async def test_a_failed_revision_still_reports_the_grade_and_records_the_debt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mastery is authoritative and already committed; the derived plan carries the failure."""
    learner, subject, root, _dependent = await _graph(db_session)
    # The repair path rolls the session back, expiring loaded rows — hold plain ids, not ORM
    # objects, across the call (the states answer_item returns are re-read for exactly this).
    learner_id, subject_id = learner.id, subject.id
    await _due_review_state(db_session, learner_id, root.id)
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner_id, subject_id=subject_id, goal=None
    )
    item = await _mcq_item(db_session, root.id)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("revision exploded")

    monkeypatch.setattr(assessment_svc.lesson_plan_svc, "revise_plan", boom)
    result, states = await assessment_svc.answer_item(
        db_session, learner_id, item, AnswerSubmit(response={"choice": 0}), llm=fake_llm_client()
    )

    # The learner is told what they earned, once.
    assert result.score == 1.0 and len(states) == 1
    events = (
        await db_session.scalars(
            select(LearningEvent).where(LearningEvent.learner_id == learner_id)
        )
    ).all()
    assert len(events) == 1

    plan = await svc._get_plan(db_session, learner_id, subject_id)
    assert plan is not None and plan.revision_pending is True


async def test_a_pending_plan_repairs_itself_on_the_next_read(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan catches up without the learner having to answer anything else."""
    learner, subject, root, _dependent = await _graph(db_session)
    learner_id, subject_id, root_id = learner.id, subject.id, root.id
    await _due_review_state(db_session, learner_id, root_id)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner_id, subject_id=subject_id, goal=None
    )
    assert plan.steps[0]["status"] == "active"
    item = await _mcq_item(db_session, root_id)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("revision exploded")

    monkeypatch.setattr(assessment_svc.lesson_plan_svc, "revise_plan", boom)
    await assessment_svc.answer_item(
        db_session, learner_id, item, AnswerSubmit(response={"choice": 0}), llm=fake_llm_client()
    )
    monkeypatch.undo()

    repaired = await svc.get_lesson_plan(db_session, learner_id, subject_id)
    assert repaired is not None
    assert repaired.revision_pending is False
    review_step = next(
        s for s in repaired.steps if s["kc_id"] == str(root_id) and s["step_type"] == "review"
    )
    assert review_step["status"] == "done"  # the revision the failed one owed


async def test_a_healthy_read_still_does_not_recompute(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair-on-read must not turn every plan read into a revision."""
    learner, subject, _root, _dependent = await _graph(db_session)
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    async def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_lesson_plan recomputed a plan that owed nothing")

    monkeypatch.setattr(svc, "revise_plan", boom)
    assert await svc.get_lesson_plan(db_session, learner.id, subject.id) is not None


# --- a goal bigger than the step cap is worked through, not truncated (S63) ---


async def _chain(db_session: AsyncSession, length: int) -> tuple[Learner, Subject, list[KC]]:
    """A prerequisite chain kc0 -> kc1 -> ... -> kc(n-1), so topo order is exactly that."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Long")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    kcs = [KC(topic_id=topic.id, slug=f"kc-{i:03d}", name=f"KC {i}") for i in range(length)]
    db_session.add_all(kcs)
    await db_session.flush()
    for prereq, dependent in itertools.pairwise(kcs):
        db_session.add(KCEdge(kc_id=dependent.id, prereq_kc_id=prereq.id))
    await db_session.flush()
    return learner, subject, kcs


async def test_a_goal_deeper_than_the_cap_records_the_whole_objective(
    db_session: AsyncSession,
) -> None:
    learner, subject, kcs = await _chain(db_session, get_settings().lesson_plan_max_steps + 5)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    assert len(plan.steps) == get_settings().lesson_plan_max_steps  # the horizon is still capped
    assert plan.objective_kc_count == len(kcs)  # but the goal is not
    assert plan.deferred_kc_count == 5
    # The last KC — the one everything else is a prerequisite for — is not in the window yet.
    assert str(kcs[-1].id) not in {s["kc_id"] for s in plan.steps}


async def test_finishing_the_window_advances_it_instead_of_ending_the_plan(
    db_session: AsyncSession,
) -> None:
    """Completing the first twenty prerequisites must not read as completing the goal."""
    cap = get_settings().lesson_plan_max_steps
    learner, subject, kcs = await _chain(db_session, cap + 5)
    await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    for kc in kcs[:cap]:
        await _mastered_state(db_session, learner.id, kc.id)

    revised = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)
    assert revised is not None
    planned = {s["kc_id"] for s in revised.steps}
    assert str(kcs[-1].id) in planned  # the actual target is now in front of the learner
    assert revised.deferred_kc_count == 0
    assert any(s["status"] == "active" for s in revised.steps)  # and the plan is not "finished"


async def test_a_goal_inside_the_cap_defers_nothing(db_session: AsyncSession) -> None:
    learner, subject, _kcs = await _chain(db_session, 3)
    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    assert plan.objective_kc_count == 3 and plan.deferred_kc_count == 0
