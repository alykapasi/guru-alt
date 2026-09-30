"""A provider refusal takes the path a spend refusal already takes (S49)."""

import contextlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelRole
from app.llm.meter import PROVIDER_MESSAGES, ProviderUnavailable
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.models.chat import Conversation, Message
from app.models.learner import Learner


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
