"""Assessment service + API: authoring items and the answer→grade→trace loop."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import LLMCall
from app.models.knowledge import KC, Subject, Topic
from app.models.learning import LearnerKCState, LearningEvent

API = "/api/v1"
RUBRIC_REPLY = '{"score": 0.75, "rationale": "Good, with minor gaps."}'


@pytest.fixture
def fake_grader() -> Iterator[None]:
    """Route every model role to a FakeProvider that returns a canned rubric grade."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(reply=RUBRIC_REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def _seed_kcs(session: AsyncSession, n: int = 1) -> list[KC]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [KC(topic_id=topic.id, slug=f"kc{i}", name=f"kc{i}") for i in range(n)]
    session.add_all(kcs)
    await session.flush()
    return kcs


def _mcq_body(kc_id: uuid.UUID) -> dict:
    return {
        "item_type": "mcq",
        "stem": "2 + 2 = ?",
        "kcs": [{"kc_id": str(kc_id)}],
        "answer_key": {"choices": ["3", "4", "5"], "correct": 1},
        "difficulty": 0.0,
    }


# --- authoring --------------------------------------------------------------


async def test_create_item_hides_answer_key(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    (kc,) = await _seed_kcs(db_session)
    r = await api_client.post(f"{API}/items", json=_mcq_body(kc.id))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["item_type"] == "mcq"
    assert body["kcs"][0]["kc_id"] == str(kc.id)
    assert "answer_key" not in body  # never leak the key to a learner


async def test_get_item_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/items/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_objective_item_requires_answer_key_422(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = _mcq_body(kc.id)
    del body["answer_key"]
    r = await api_client.post(f"{API}/items", json=body)
    assert r.status_code == 422


async def test_create_item_unknown_kc_400(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/items", json=_mcq_body(uuid.uuid4()))
    assert r.status_code == 400


# --- the answer loop --------------------------------------------------------


async def test_answer_correct_raises_mastery(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    (kc,) = await _seed_kcs(db_session)
    item_id = (await api_client.post(f"{API}/items", json=_mcq_body(kc.id))).json()["id"]

    r = await api_client.post(f"{API}/items/{item_id}/answer", json={"response": {"choice": 1}})
    assert r.status_code == 200, r.text
    grade = r.json()
    assert grade["score"] == 1.0
    assert grade["correct"] is True
    (est,) = grade["estimates"]
    assert est["kc_id"] == str(kc.id)
    assert est["ability"] > 0.0
    assert est["uncertainty"] < 1.0


async def test_answer_wrong_lowers_mastery(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    (kc,) = await _seed_kcs(db_session)
    item_id = (await api_client.post(f"{API}/items", json=_mcq_body(kc.id))).json()["id"]
    grade = (
        await api_client.post(f"{API}/items/{item_id}/answer", json={"response": {"choice": 0}})
    ).json()
    assert grade["score"] == 0.0
    assert grade["estimates"][0]["ability"] < 0.0


async def test_answer_persists_state_and_event(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    (kc,) = await _seed_kcs(db_session)
    item_id = (await api_client.post(f"{API}/items", json=_mcq_body(kc.id))).json()["id"]
    await api_client.post(
        f"{API}/items/{item_id}/answer",
        json={"response": {"choice": 1}, "latency_ms": 4200, "hints_used": 1},
    )

    state = await db_session.scalar(select(LearnerKCState).where(LearnerKCState.kc_id == kc.id))
    assert state is not None and state.ability > 0.0

    event = await db_session.scalar(select(LearningEvent).where(LearningEvent.kc_id == kc.id))
    assert event is not None
    assert event.payload["score"] == 1.0
    assert event.payload["item_id"] == item_id
    assert event.payload["response"] == {"choice": 1}
    assert event.payload["latency_ms"] == 4200


async def test_multi_kc_item_traces_every_kc(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    a, b = await _seed_kcs(db_session, n=2)
    body = {
        "item_type": "fill_blank",
        "stem": "fill these",
        "kcs": [{"kc_id": str(a.id), "weight": 1.0}, {"kc_id": str(b.id), "weight": 1.0}],
        "answer_key": {"blanks": ["x", "y"]},
    }
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    grade = (
        await api_client.post(
            f"{API}/items/{item_id}/answer", json={"response": {"blanks": ["x", "y"]}}
        )
    ).json()
    assert grade["score"] == 1.0
    assert {e["kc_id"] for e in grade["estimates"]} == {str(a.id), str(b.id)}


async def test_flashcard_self_graded(api_client: AsyncClient, db_session: AsyncSession) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "flashcard", "stem": "Capital of France?", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    r = await api_client.post(f"{API}/items/{item_id}/answer", json={"response": {"rating": 4}})
    assert r.status_code == 200, r.text
    grade = r.json()
    assert grade["score"] == 1.0
    assert grade["estimates"][0]["ability"] > 0.0

    # Answering scheduled a future review, so nothing is due yet.
    due = await db_session.scalar(select(LearnerKCState).where(LearnerKCState.kc_id == kc.id))
    assert due is not None and due.due_at is not None and due.fsrs_card is not None


async def test_flashcard_bad_rating_422(api_client: AsyncClient, db_session: AsyncSession) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "flashcard", "stem": "Capital of France?", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    r = await api_client.post(f"{API}/items/{item_id}/answer", json={"response": {"foo": "bar"}})
    assert r.status_code == 422


async def test_due_reviews_endpoint(api_client: AsyncClient, db_session: AsyncSession) -> None:
    (kc,) = await _seed_kcs(db_session)
    item_id = (await api_client.post(f"{API}/items", json=_mcq_body(kc.id))).json()["id"]
    # Brand-new learner: nothing is due before any review.
    assert (await api_client.get(f"{API}/reviews/due")).json() == []

    await api_client.post(f"{API}/items/{item_id}/answer", json={"response": {"choice": 1}})
    # Force the scheduled review into the past so it surfaces as due.
    state = await db_session.scalar(select(LearnerKCState).where(LearnerKCState.kc_id == kc.id))
    assert state is not None
    state.due_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()

    due = (await api_client.get(f"{API}/reviews/due")).json()
    assert [r["kc_id"] for r in due] == [str(kc.id)]
    assert "ability" in due[0] and "due_at" in due[0]


# --- rubric (LLM) grading ---------------------------------------------------


async def test_short_item_rubric_graded(
    api_client: AsyncClient, db_session: AsyncSession, fake_grader: None
) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "short", "stem": "Explain photosynthesis.", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]

    r = await api_client.post(
        f"{API}/items/{item_id}/answer",
        json={"response": {"text": "Plants convert light into chemical energy."}},
    )
    assert r.status_code == 200, r.text
    grade = r.json()
    assert grade["score"] == 0.75
    assert grade["correct"] is True
    assert grade["detail"]["rationale"] == "Good, with minor gaps."
    (est,) = grade["estimates"]
    assert est["kc_id"] == str(kc.id)
    assert est["ability"] > 0.0  # 0.75 beats the 0.5 baseline → mastery rises


async def test_rubric_grade_persists_state_event_and_llm_call(
    api_client: AsyncClient, db_session: AsyncSession, fake_grader: None
) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "long", "stem": "Discuss entropy.", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    await api_client.post(
        f"{API}/items/{item_id}/answer", json={"response": {"text": "Entropy measures disorder."}}
    )

    state = await db_session.scalar(select(LearnerKCState).where(LearnerKCState.kc_id == kc.id))
    assert state is not None and state.ability > 0.0

    event = await db_session.scalar(select(LearningEvent).where(LearningEvent.kc_id == kc.id))
    assert event is not None
    assert event.payload["score"] == 0.75
    assert event.payload["response"] == {"text": "Entropy measures disorder."}

    call = await db_session.scalar(select(LLMCall).where(LLMCall.role == "smart"))
    assert call is not None
    assert call.model and call.output_tokens > 0


async def test_empty_open_response_scores_zero(
    api_client: AsyncClient, db_session: AsyncSession, fake_grader: None
) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "short", "stem": "Define inertia.", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    grade = (
        await api_client.post(f"{API}/items/{item_id}/answer", json={"response": {"text": "  "}})
    ).json()
    assert grade["score"] == 0.0
    assert grade["estimates"][0]["ability"] < 0.0  # empty answer lowers mastery


async def test_answer_missing_item_404(api_client: AsyncClient) -> None:
    r = await api_client.post(
        f"{API}/items/{uuid.uuid4()}/answer", json={"response": {"choice": 0}}
    )
    assert r.status_code == 404
