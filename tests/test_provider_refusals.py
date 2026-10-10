"""A provider refusal takes the path a spend refusal already takes (S49)."""

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm import ModelRole
from app.llm.meter import PROVIDER_MESSAGES, ProviderUnavailable
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.main import app
from app.models.chat import Conversation, Message, Turn, TurnStatus
from app.models.learner import Learner
from app.services.turn_common import TurnEvent, refusal_ends_turn


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def _learner(session: AsyncSession) -> uuid.UUID:
    learner = Learner(handle=f"r-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner.id


async def test_a_reply_the_provider_drops_says_why(db_session: AsyncSession) -> None:
    """Generation's own error handler must not turn a refusal into "generation failed"."""
    from app.services import chat as chat_svc

    learner_id = await _learner(db_session)
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.commit()
    llm = _client(
        FakeProvider(
            refuse=ProviderUnavailable("down"),
            refuse_calls=frozenset({"stream"}),
            refuse_after_words=1,
        )
    )

    events = [
        e
        async for e in chat_svc.run_tutor_turn(
            db_session,
            llm,
            learner_id=learner_id,
            conversation=conversation,
            history=[],
            user_content="hello",
            max_tokens=200,
            source_ids=[],
        )
    ]

    assert [e.detail for e in events if e.type == "error"] == [PROVIDER_MESSAGES["down"]]


async def test_background_work_the_provider_refused_is_deferred(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.workers import tasks

    learner_id = await _learner(db_session)

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    monkeypatch.setattr(
        tasks,
        "build_llm_client",
        lambda _s: _client(FakeProvider(refuse=ProviderUnavailable("busy", retry_after=5.0))),
    )
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.flush()
    # A message, so the interests estimator makes a model call.
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I love chess."))
    await db_session.commit()
    logged: list[str] = []
    monkeypatch.setattr(tasks.logger, "info", lambda msg, *a: logged.append(msg % a))

    await tasks._profile_refresh_task(str(learner_id))  # must not raise

    assert logged == ["provider.deferred task=profile_refresh"]


async def test_an_ingestion_the_provider_refused_carries_the_message(
    db_session: AsyncSession,
) -> None:
    from app.models.source import SourceKind
    from app.services import ingestion
    from app.storage import InMemoryBlobStore

    learner_id = await _learner(db_session)
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner_id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=b"Photosynthesis turns light into chemical energy in the chloroplast.",
    )
    await db_session.commit()
    llm = _client(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"embed"}))
    )

    result = await ingestion.ingest_source(db_session, store, llm, source.id)

    assert result is not None
    assert result.error == PROVIDER_MESSAGES["down"]


API = "/api/v1"


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client("A whole reply.")
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _sse(body: str) -> list[dict]:
    return [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]


async def _conversation(api_client: AsyncClient, db_session: AsyncSession) -> str:
    cid = (await api_client.post(f"{API}/conversations", json={"title": "Calc"})).json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(cid))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()
    return cid


def _busy_flow():
    """Streams a word, then the provider says it is busy."""

    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="Half")
        raise ProviderUnavailable("busy", retry_after=7.0)

    return refusal_ends_turn(flow)


async def test_a_busy_provider_ends_the_turn_saying_so(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _busy_flow())

    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    assert _sse(r.text)[-1] == {
        "type": "error",
        "detail": PROVIDER_MESSAGES["busy"],
        "code": "provider_busy",
        "retry_after": 7.0,
    }
    turn = (
        await db_session.scalars(select(Turn).where(Turn.conversation_id == uuid.UUID(cid)))
    ).one()
    assert (turn.status, turn.error) == (TurnStatus.FAILED, "provider_busy")
    replies = await db_session.scalars(
        select(Message).where(
            Message.conversation_id == uuid.UUID(cid), Message.role == "assistant"
        )
    )
    assert replies.all() == []  # the half-written reply is not kept


async def test_a_turn_the_provider_refused_can_be_retried(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    key = str(uuid.uuid4())
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _busy_flow())
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )
    monkeypatch.undo()

    r = await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )

    assert _sse(r.text)[-1]["type"] == "done"


async def test_a_route_over_a_busy_provider_answers_503_with_the_wait(
    api_client: AsyncClient, api_learner: Learner, db_session: AsyncSession
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I love chess."))
    await db_session.commit()
    app.dependency_overrides[get_llm_client] = lambda: _client(
        FakeProvider(refuse=ProviderUnavailable("busy", retry_after=7.5))
    )
    try:
        r = await api_client.post(f"{API}/profile/refresh")
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert r.status_code == 503
    assert r.headers["retry-after"] == "8"
    assert r.json() == {
        "detail": {
            "code": "provider_busy",
            "message": PROVIDER_MESSAGES["busy"],
            "retry_after": 7.5,
        }
    }
