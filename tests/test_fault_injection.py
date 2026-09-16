"""What the system is left holding when a dependency fails halfway through (S58).

Every failure test in the suite before this one fails a dependency *before* it does anything:
the stream raises on its first chunk, the queue refuses the dispatch, the bucket is empty.
Those establish that an exception propagates. They cannot establish the thing that actually
costs money and trust, because none of them ever reaches it — the state where real work is
already done, already partly paid for, and already partly on the learner's screen.

Each test here injects its fault mid-operation and asserts that the fault fired, because a
fault that quietly failed to inject reports as a pass.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.llm import ModelRole
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import Usage
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import pipeline
from app.rag.pipeline import PartialEmbedding, embed_in_batches
from app.services import blob_integrity, ingestion
from app.services.chat import run_tutor_turn
from app.storage import InMemoryBlobStore
from tests.faults import DyingStream, FailingEmbedBatch, FaultyBlobStore, InjectedFault

# Long enough to chunk into six pieces, so batch three fails with batches on either side of it.
TEXT = (
    "Photosynthesis converts light energy into chemical energy stored in glucose. "
    "The light-dependent reactions occur in the thylakoid membranes of the chloroplast. "
    "Water is split, oxygen is released, and ATP and NADPH are produced for the next stage. "
    "The Calvin cycle then fixes carbon dioxide into a three-carbon sugar using that ATP. "
    "Rubisco catalyses the fixing step and is the most abundant enzyme on the planet. "
    "Its affinity for oxygen as well as carbon dioxide makes photorespiration unavoidable. "
    "C4 and CAM plants evolved separate strategies to concentrate carbon dioxide around it. "
) * 8


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _uploaded(session: AsyncSession, store: Any) -> Source:
    """A real upload: the bytes are in the store and the row references them."""
    return await ingestion.create_source(
        session,
        store,
        learner_id=(await _learner(session)).id,
        kind=SourceKind.FILE,
        origin="photosynthesis.txt",
        content_type="text/plain",
        data=TEXT.encode(),
    )


def _one_chunk_per_batch() -> Settings:
    """Every chunk its own embed call, so "the third batch" is something a test can point at."""
    return get_settings().model_copy(update={"embed_batch_size": 1, "embed_concurrency": 2})


def _broken_embedder(on_call: int) -> tuple[LLMClient, FailingEmbedBatch]:
    provider = FailingEmbedBatch(on_call=on_call)
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    return client, provider


async def _chunk_count(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(select(func.count(Chunk.id)).where(Chunk.source_id == source_id))
    ) or 0


# --- a provider that dies with the reply half-written -------------------------------------


async def test_a_stream_that_dies_after_three_tokens_leaves_no_half_written_reply(
    db_session: AsyncSession,
) -> None:
    """The learner has already read three words. None of them may reach the transcript.

    A partial reply persisted here is worse than an error: it comes back on reload as something
    the tutor said, indistinguishable from a complete answer that happens to stop mid-sentence,
    and every later turn is conditioned on it.
    """
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.commit()

    provider = DyingStream("A derivative measures an instantaneous rate of change.", after=3)
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})

    events = [
        ev
        async for ev in run_tutor_turn(
            db_session,
            client,
            learner_id=learner.id,
            conversation=conversation,
            history=[],
            user_content="What is a derivative?",
            max_tokens=256,
        )
    ]

    assert provider.delivered == 3, "the fault did not fire mid-stream"
    assert len([e for e in events if e.type == "token"]) == 3  # the learner saw them
    assert events[-1].type == "error"

    messages = (
        await db_session.scalars(select(Message).where(Message.conversation_id == conversation.id))
    ).all()
    assert [m.role for m in messages] == ["user"]
    assert not any("derivative measures" in (m.content or "") for m in messages)


# --- a provider that fails after the money has gone ---------------------------------------


async def test_a_batch_failing_partway_still_reports_what_the_other_batches_cost() -> None:
    """`asyncio.gather` abandons its siblings on the first exception, so the batches that
    succeeded were billed and their usage was thrown away with their vectors."""
    client, provider = _broken_embedder(on_call=3)

    with pytest.raises(PartialEmbedding) as caught:
        await embed_in_batches(
            client, [f"chunk number {i}" for i in range(5)], batch_size=1, concurrency=2
        )

    assert provider.fired == 1, "the fault did not fire"
    assert provider.calls == 5, "the other batches were abandoned rather than settled"
    assert caught.value.usage.input_tokens == provider.billed.input_tokens > 0
    assert isinstance(caught.value.__cause__, InjectedFault)


async def test_a_failed_embedding_writes_no_chunks_at_all(db_session: AsyncSession) -> None:
    """Half an embedded corpus is the expensive outcome: retrieval keeps working, quietly
    answering out of whichever fraction of the document made it in."""
    store = InMemoryBlobStore()
    source = await _uploaded(db_session, store)
    client, provider = _broken_embedder(on_call=3)

    result = await ingestion.ingest_source(
        db_session, store, client, source.id, settings=_one_chunk_per_batch()
    )

    assert provider.fired == 1, "the fault did not fire"
    assert provider.calls > 3, "the fault fired on the last batch, not partway through"
    assert result is not None and result.status != SourceStatus.DONE
    assert result.error and "embedding failed" in result.error
    assert await _chunk_count(db_session, source.id) == 0


async def test_the_batches_already_billed_are_sent_to_accounting_before_the_failure_propagates(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry loop that spends real money and records none of it is invisible to the budget
    watch (P11): the source rolls back, the job runs again, and the provider's invoice grows
    while the account looks quiet.

    Accounting is captured here rather than read back out of the database, because the suite's
    fixture routes it onto the test's own connection — where ``ingest_source``'s rollback
    discards it, which production's separate transaction would not. That the row survives a
    rollback is ``test_llm_log`` 's property; that the usage gets there at all is this one's.
    """
    store = InMemoryBlobStore()
    source = await _uploaded(db_session, store)
    client, provider = _broken_embedder(on_call=3)
    recorded: list[tuple[str, Usage]] = []

    async def _record(*, role: str, usage: Usage, **_: Any) -> float | None:
        recorded.append((role, usage))
        return None

    monkeypatch.setattr(pipeline, "log_llm_call", _record)

    await ingestion.ingest_source(
        db_session, store, client, source.id, settings=_one_chunk_per_batch()
    )

    assert provider.fired == 1, "the fault did not fire"
    embeds = [usage for role, usage in recorded if role == str(ModelRole.EMBED)]
    assert len(embeds) == 1
    assert embeds[0].input_tokens == provider.billed.input_tokens > 0


async def test_the_source_ingests_cleanly_once_the_provider_recovers(
    db_session: AsyncSession,
) -> None:
    """The point of leaving no chunks behind: the retry is a fresh run, not a repair."""
    store = InMemoryBlobStore()
    source = await _uploaded(db_session, store)
    settings = _one_chunk_per_batch()
    broken, _ = _broken_embedder(on_call=3)
    await ingestion.ingest_source(db_session, store, broken, source.id, settings=settings)

    await ingestion.reset_for_reingest(db_session, source.id)
    healed = await ingestion.ingest_source(
        db_session, store, fake_llm_client(), source.id, settings=settings
    )

    assert healed is not None and healed.status == SourceStatus.DONE
    count = await _chunk_count(db_session, source.id)
    assert count > 0
    assert count == healed.meta["chunk_count"], "the failed attempt left chunks behind"


# --- a store that fails partway through ---------------------------------------------------


async def test_a_store_that_fails_on_read_leaves_the_source_diagnosable_not_silently_empty(
    db_session: AsyncSession,
) -> None:
    inner = InMemoryBlobStore()
    source = await _uploaded(db_session, inner)
    store = FaultyBlobStore(inner, method="download")

    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert store.fired == 1, "the fault did not fire"
    assert result is not None and result.status != SourceStatus.DONE
    assert result.error and "object store failed" in result.error
    assert await _chunk_count(db_session, source.id) == 0


async def test_a_key_the_store_will_not_answer_for_is_neither_present_nor_missing(
    db_session: AsyncSession,
) -> None:
    """Three keys, and the store errors on the second. Calling it missing raises a data-loss
    alarm over a network blip; calling it present passes a restore nobody verified. It is
    neither, and the walk has to finish and say so — this used to abandon the whole drill."""
    inner = InMemoryBlobStore()
    learner = await _learner(db_session)
    keys = [f"blobs/{name}" for name in ("a", "b", "c")]
    for key in keys:
        await inner.put(key, b"bytes")
        db_session.add(
            Source(
                learner_id=learner.id,
                kind=SourceKind.FILE,
                origin=f"{key}.pdf",
                status=SourceStatus.DONE,
                content_type="application/pdf",
                blob_key=key,
                meta={},
            )
        )
    await db_session.flush()
    store = FaultyBlobStore(inner, method="exists", on_call=2)

    report = await blob_integrity.check(db_session, store)

    assert store.fired == 1, "the fault did not fire"
    assert report.checked == 3, "the walk stopped at the bad key instead of finishing"
    assert report.missing == []
    assert [u.blob_key for u in report.unreadable] == [keys[1]]
    assert "object store failed" in report.unreadable[0].error
    assert not report.intact, "a walk that could not read the bucket has verified nothing"
