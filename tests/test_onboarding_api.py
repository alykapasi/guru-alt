"""Onboarding API endpoints tests: goal refinement and curriculum generation."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.api.deps import get_llm_client
from app.llm.registry import fake_llm_client
from app.main import app

API = "/api/v1"

# Mock LLM responses for tests
GOAL_REFINEMENT_REPLY = "Sounds like you want to learn about photosynthesis."
CURRICULUM_REPLY = """{
    "subject_name": "Biology",
    "subject_description": "The study of life and living organisms",
    "topics": [
        {
            "name": "Photosynthesis Basics",
            "description": "Introduction to photosynthesis",
            "kcs": [
                {"name": "Light Reactions", "description": "The light-dependent reactions"},
                {"name": "Dark Reactions", "description": "The light-independent reactions (Calvin cycle)"}
            ]
        },
        {
            "name": "Chloroplast Structure",
            "description": "Understanding the structure of chloroplasts",
            "kcs": [
                {"name": "Thylakoid", "description": "Flattened sacs containing pigments"},
                {"name": "Stroma", "description": "Fluid-filled space inside chloroplast"}
            ]
        }
    ]
}"""


def _parse_sse(text: str) -> list[dict]:
    """Parse SSE response body into list of event dicts."""
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def fake_llm_goal() -> Iterator[None]:
    """Override LLM with goal refinement response."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(GOAL_REFINEMENT_REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


@pytest.fixture
def fake_llm_curriculum() -> Iterator[None]:
    """Override LLM with curriculum response."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(CURRICULUM_REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


@pytest.fixture
def fake_llm_invalid() -> Iterator[None]:
    """Override LLM with invalid JSON response."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client("not json at all")
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def test_goal_turns_streams_sse(api_client: AsyncClient, fake_llm_goal: None) -> None:
    """Test goal refinement endpoint streams SSE frames."""
    session_id = str(uuid.uuid4())
    response = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={
            "session_id": session_id,
            "content": "I want to learn photosynthesis",
            "satisfied": False,
            "mode": "start",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    assert len(events) > 0

    # Should have token events
    token_events = [e for e in events if e["type"] == "token"]
    assert len(token_events) > 0
    # Reconstruct the full text from tokens
    full_text = "".join(e["text"] for e in token_events)
    assert GOAL_REFINEMENT_REPLY in full_text

    # Should have awaiting_reply event
    awaiting = next((e for e in events if e["type"] == "awaiting_reply"), None)
    assert awaiting is not None
    assert "awaiting_reply" in str(awaiting["type"])


async def test_goal_turns_rejects_unknown_mode(api_client: AsyncClient) -> None:
    """An invalid mode is rejected with 422 rather than silently falling through to 'start'."""
    response = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={
            "session_id": str(uuid.uuid4()),
            "content": "I want to learn photosynthesis",
            "satisfied": False,
            "mode": "resmue",
        },
    )
    assert response.status_code == 422


async def test_curriculum_endpoint_returns_proposal(
    api_client: AsyncClient, fake_llm_curriculum: None
) -> None:
    """Test curriculum endpoint returns a valid proposal."""
    response = await api_client.post(
        f"{API}/onboarding/curriculum",
        json={"goal": "Learn about photosynthesis", "source_ids": None},
    )

    assert response.status_code == 200
    data = response.json()

    assert data["subject_name"] == "Biology"
    assert data["subject_description"] == "The study of life and living organisms"
    assert len(data["topics"]) == 2
    assert data["topics"][0]["name"] == "Photosynthesis Basics"
    assert len(data["topics"][0]["kcs"]) == 2


async def test_curriculum_endpoint_400_on_llm_failure(
    api_client: AsyncClient, fake_llm_invalid: None
) -> None:
    """Test curriculum endpoint returns 400 on LLM failure."""
    response = await api_client.post(
        f"{API}/onboarding/curriculum",
        json={"goal": "Learn about photosynthesis", "source_ids": None},
    )

    assert response.status_code == 400
    data = response.json()
    assert data["detail"] == "Curriculum generation failed. Please try again."
