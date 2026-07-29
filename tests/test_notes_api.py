"""Notes API: endpoint mechanics over the transactional test app."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE, get_llm_client
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.learner import Learner
from app.models.learning import LearningEvent

API = "/api/v1"

ATOMS_REPLY = json.dumps(
    {
        "atoms": [
            {
                "kind": "concept",
                "kc_ids": [],
                "md": "Sine is opposite/hypotenuse.",
                "provenance": {},
            }
        ]
    }
)
RENDERED = "# Trig\n\nSine is opposite over hypotenuse."

EDITED_ATOMS_REPLY = json.dumps(
    {
        "atoms": [
            {
                "kind": "concept",
                "kc_ids": [],
                "md": "Sine is opposite/hypotenuse.",
                "provenance": {},
            },
            {
                "kind": "learner",
                "kc_ids": [],
                "md": "My mnemonic: SOH.",
                "provenance": {},
            },
        ]
    }
)
EDITED_RENDERED = "# Trig\n\nSine is opposite over hypotenuse.\n\nSOH mnemonic added."
NARRATIVE_RENDERED = "Sine, told as a story: it's the opposite side over the hypotenuse."


@pytest.fixture
def fake_llm_refresh() -> Iterator[None]:
    """Distill reply then render reply — one full refresh."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(
        script=[FakeTurn(text=ATOMS_REPLY), FakeTurn(text=RENDERED)]
    )
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _override_llm(script: list[FakeTurn]) -> None:
    """Swap in a freshly scripted fake LLM for the next call(s). Cleared by api_client teardown."""
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(script=script)


async def _subject_topic_kc(api_client: AsyncClient) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    r = await api_client.post(
        f"{API}/subjects", json={"slug": f"s-{suffix}", "name": f"S {suffix}"}
    )
    subject_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/subjects/{subject_id}/topics", json={"slug": f"t-{suffix}", "name": "Trig"}
    )
    topic_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/topics/{topic_id}/kcs", json={"slug": f"k-{suffix}", "name": "Sine"}
    )
    return subject_id, topic_id, r.json()["id"]


async def _make_stale(db_session: AsyncSession, kc_id: str) -> None:
    """Insert an observation for the request-scoped dev learner (created by the first request)."""
    learner = await db_session.scalar(select(Learner).where(Learner.handle == DEV_LEARNER_HANDLE))
    assert learner is not None
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=uuid.UUID(kc_id),
            event_type="observation",
            payload={"score": 0.0, "hints_used": 0, "item_id": None, "response": None},
        )
    )
    await db_session.flush()


async def test_get_note_empty_not_stale(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.get(f"{API}/topics/{topic_id}/note")
    assert r.status_code == 200
    data = r.json()
    assert data["content_md"] is None and data["stale"] is False
    assert data["effective_format"] == "outline"


async def test_refresh_distills_and_renders(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_refresh: None
) -> None:
    subject_id, topic_id, kc_id = await _subject_topic_kc(api_client)
    await _make_stale(db_session, kc_id)

    r = await api_client.get(f"{API}/topics/{topic_id}/note")
    assert r.json()["stale"] is True

    r = await api_client.post(f"{API}/topics/{topic_id}/note/refresh")
    assert r.status_code == 200
    data = r.json()
    assert data["content_md"] == RENDERED
    assert data["revision_ordinal"] == 1 and data["stale"] is False

    r = await api_client.get(f"{API}/subjects/{subject_id}/notes")
    entries = r.json()
    assert entries[0]["has_note"] is True and entries[0]["stale"] is False

    r = await api_client.get(f"{API}/topics/{topic_id}/note/revisions")
    assert [rev["cause"] for rev in r.json()] == ["distill"]


async def test_edit_404_without_note(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.put(f"{API}/topics/{topic_id}/note", json={"content_md": "hi"})
    assert r.status_code == 404


async def test_format_patch_validates(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.patch(f"{API}/topics/{topic_id}/note/format", json={"format": "haiku"})
    assert r.status_code == 422


async def test_unknown_topic_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/topics/{uuid.uuid4()}/note")
    assert r.status_code == 404


async def test_restore_unknown_revision_404(api_client: AsyncClient) -> None:
    _, topic_id, _ = await _subject_topic_kc(api_client)
    r = await api_client.post(f"{API}/topics/{topic_id}/note/revisions/7/restore")
    assert r.status_code == 404


async def test_format_patch_success_sets_and_renders(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_refresh: None
) -> None:
    """A note with a current revision: switching format renders (and caches) the new format."""
    _, topic_id, kc_id = await _subject_topic_kc(api_client)
    await _make_stale(db_session, kc_id)
    r = await api_client.post(f"{API}/topics/{topic_id}/note/refresh")
    assert r.status_code == 200

    _override_llm([FakeTurn(text=NARRATIVE_RENDERED)])
    r = await api_client.patch(f"{API}/topics/{topic_id}/note/format", json={"format": "narrative"})
    assert r.status_code == 200
    data = r.json()
    assert data["format"] == "narrative"
    assert data["effective_format"] == "narrative"
    assert data["content_md"] == NARRATIVE_RENDERED


async def test_edit_note_success_creates_learner_edit_revision(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_refresh: None
) -> None:
    _, topic_id, kc_id = await _subject_topic_kc(api_client)
    await _make_stale(db_session, kc_id)
    r = await api_client.post(f"{API}/topics/{topic_id}/note/refresh")
    assert r.status_code == 200

    _override_llm([FakeTurn(text=EDITED_ATOMS_REPLY), FakeTurn(text=EDITED_RENDERED)])
    r = await api_client.put(f"{API}/topics/{topic_id}/note", json={"content_md": "my edited note"})
    assert r.status_code == 200
    data = r.json()
    assert data["revision_ordinal"] == 2
    assert data["content_md"] == EDITED_RENDERED

    r = await api_client.get(f"{API}/topics/{topic_id}/note/revisions")
    assert [rev["cause"] for rev in r.json()] == ["distill", "learner_edit"]


async def test_restore_revision_success_creates_new_revision(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_refresh: None
) -> None:
    _, topic_id, kc_id = await _subject_topic_kc(api_client)
    await _make_stale(db_session, kc_id)
    r = await api_client.post(f"{API}/topics/{topic_id}/note/refresh")
    assert r.status_code == 200

    _override_llm([FakeTurn(text=EDITED_ATOMS_REPLY), FakeTurn(text=EDITED_RENDERED)])
    r = await api_client.put(f"{API}/topics/{topic_id}/note", json={"content_md": "my edited note"})
    assert r.status_code == 200

    # Restore back to revision 1 (pre-edit): copies forward as a NEW revision, never rewrites.
    _override_llm([FakeTurn(text=RENDERED)])
    r = await api_client.post(f"{API}/topics/{topic_id}/note/revisions/1/restore")
    assert r.status_code == 200
    data = r.json()
    assert data["revision_ordinal"] == 3
    assert data["content_md"] == RENDERED


async def test_revision_source_success_and_404(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_refresh: None
) -> None:
    _, topic_id, kc_id = await _subject_topic_kc(api_client)
    await _make_stale(db_session, kc_id)
    r = await api_client.post(f"{API}/topics/{topic_id}/note/refresh")
    assert r.status_code == 200

    r = await api_client.get(f"{API}/topics/{topic_id}/note/revisions/1")
    assert r.status_code == 200
    data = r.json()
    assert data["ordinal"] == 1
    assert "## Concepts" in data["content_md"]

    r = await api_client.get(f"{API}/topics/{topic_id}/note/revisions/999")
    assert r.status_code == 404
