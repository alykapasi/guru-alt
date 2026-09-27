"""Memory correction and forgetting are durable (S42).

Three things were lost. A near-duplicate was *skipped*, so a learner correcting a preference
had the correction discarded and the stale entry kept — precisely backwards. A deleted memory
was hard-deleted, so the next write-back over the same conversation re-extracted it and it came
back. And retrieval returned the nearest entries with no floor, so a learner with a handful of
memories had all of them injected into every turn regardless of the question.
"""

import contextlib
import uuid
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.registry import fake_llm_client
from app.memory import retrieval
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind, MemoryStatus
from app.services import memory as svc
from tests.embedding import FAKE_SPACE

MORNINGS = '{"memories": [{"kind": "preference", "content": "Studies in the mornings."}]}'
EVENINGS = '{"memories": [{"kind": "preference", "content": "Studies in the evenings now."}]}'
UPDATES_FIRST = '{"verdicts": [{"candidate": 1, "verdict": "updates", "replaces": 1}]}'
COEXISTS_FIRST = '{"verdicts": [{"candidate": 1, "verdict": "coexists"}]}'
SAME_FIRST = '{"verdicts": [{"candidate": 1, "verdict": "same"}]}'


def _extract_then_judge(extracted: str, verdicts: str):
    return fake_llm_client(script=[FakeTurn(text=extracted), FakeTurn(text=verdicts)])


def _calls(llm) -> int:
    return len(cast(FakeProvider, llm._providers["fake"]).prompts_sent)


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner


async def _conversation(session: AsyncSession, learner: Learner, text: str) -> Conversation:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.flush()
    session.add(Message(conversation_id=conversation.id, role="user", content=text))
    await session.commit()
    return conversation


async def _memories(session: AsyncSession, learner_id: uuid.UUID) -> list[Memory]:
    return list(
        (
            await session.scalars(
                select(Memory).where(Memory.learner_id == learner_id).order_by(Memory.created_at)
            )
        ).all()
    )


@contextlib.contextmanager
def _everything_is_equivalent():
    """Treat any same-kind memory as the one a new extraction is about.

    The fake provider derives embeddings from a hash, so "studies in the mornings" and
    "studies in the evenings now" land nowhere near each other — real embeddings of a
    preference and its correction would. Widening the threshold isolates what these tests are
    about (skip vs supersede vs suppress) from a distance the harness cannot make meaningful.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.memory.get_settings",
            lambda: Settings(memory_dedup_max_distance=2.0, memory_related_max_distance=2.0),
        )
        yield


def _seed(learner_id: uuid.UUID, content: str, *, status=MemoryStatus.CURRENT) -> Memory:
    return Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner_id,
        kind=MemoryKind.PREFERENCE,
        content=content,
        embedding=[0.1] * 768,
        status=status,
    )


# --- correction ---------------------------------------------------------------------------


async def test_a_correction_replaces_the_entry_it_contradicts(db_session: AsyncSession) -> None:
    """The stale entry used to win, because "close enough" meant "skip"."""
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Actually I study evenings now.")

    with _everything_is_equivalent():
        created = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, UPDATES_FIRST), conversation_id=second.id
        )

    assert [m.content for m in created] == ["Studies in the evenings now."]
    current = await svc.list_memories(db_session, learner.id)
    assert [m.content for m in current] == ["Studies in the evenings now."]


async def test_the_superseded_entry_is_kept_and_points_at_its_replacement(
    db_session: AsyncSession,
) -> None:
    """A bare overwrite would discard the fact that the learner corrected something."""
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Actually I study evenings now.")
    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, UPDATES_FIRST), conversation_id=second.id
        )

    rows = await _memories(db_session, learner.id)

    superseded = [m for m in rows if m.status == MemoryStatus.SUPERSEDED]
    current = [m for m in rows if m.status == MemoryStatus.CURRENT]
    assert len(superseded) == 1 and len(current) == 1
    assert superseded[0].superseded_by_id == current[0].id


async def test_an_identical_restatement_creates_nothing(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "As I said, mornings.")

    created = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)

    assert created == []
    assert len(await svc.list_memories(db_session, learner.id)) == 1


# --- forgetting ---------------------------------------------------------------------------


async def test_a_deleted_memory_is_not_re_extracted(db_session: AsyncSession) -> None:
    """The defect: deleting a memory, then talking about it again, brought it straight back."""
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    created = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    assert await svc.delete_memory(db_session, learner.id, created[0].id) is True
    second = await _conversation(db_session, learner, "Mornings, like I said.")

    again = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)

    assert again == []
    assert await svc.list_memories(db_session, learner.id) == []


async def test_deleting_twice_is_a_no_op(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    memory = _seed(learner.id, "x")
    db_session.add(memory)
    await db_session.commit()

    assert await svc.delete_memory(db_session, learner.id, memory.id) is True
    assert await svc.delete_memory(db_session, learner.id, memory.id) is False


async def test_forget_everything_leaves_tombstones_not_holes(db_session: AsyncSession) -> None:
    """Hard-deleting in bulk is how the next write-back rebuilt what was just erased."""
    learner = await _learner(db_session)
    db_session.add_all([_seed(learner.id, "a"), _seed(learner.id, "b")])
    await db_session.commit()

    assert await svc.delete_all_memories(db_session, learner.id) == 2

    assert await svc.list_memories(db_session, learner.id) == []
    rows = await _memories(db_session, learner.id)
    assert len(rows) == 2
    assert all(m.status == MemoryStatus.DELETED for m in rows)


async def test_forgetting_everything_twice_counts_only_what_it_changed(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    db_session.add(_seed(learner.id, "a"))
    await db_session.commit()

    assert await svc.delete_all_memories(db_session, learner.id) == 1
    assert await svc.delete_all_memories(db_session, learner.id) == 0


# --- retrieval ------------------------------------------------------------------------------


async def test_retrieval_ignores_superseded_and_deleted_entries(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    db_session.add_all(
        [
            _seed(learner.id, "gone", status=MemoryStatus.DELETED),
            _seed(learner.id, "old", status=MemoryStatus.SUPERSEDED),
            _seed(learner.id, "live"),
        ]
    )
    await db_session.commit()

    hits = await retrieval.retrieve(
        db_session, fake_llm_client(), "when do they study", learner_id=learner.id
    )

    assert [h.content for h in hits] == ["live"]


async def test_a_memory_beyond_the_relevance_floor_is_not_injected(
    db_session: AsyncSession,
) -> None:
    """Without a floor, `limit` alone guarantees the nearest are returned, relevant or not."""
    learner = await _learner(db_session)
    db_session.add(_seed(learner.id, "irrelevant"))
    await db_session.commit()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.memory.retrieval.get_settings",
            lambda: Settings(memory_retrieval_max_distance=0.0),
        )
        hits = await retrieval.retrieve(
            db_session, fake_llm_client(), "something else entirely", learner_id=learner.id
        )

    assert hits == []


# --- the judge decides (S42) ----------------------------------------------------------------


async def test_coexisting_memories_both_stay(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "I also study on Sundays.")

    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, COEXISTS_FIRST), conversation_id=second.id
        )

    assert len(await svc.list_memories(db_session, learner.id)) == 2


async def test_a_rewording_is_skipped(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Mornings are when I study.")

    with _everything_is_equivalent():
        created = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, SAME_FIRST), conversation_id=second.id
        )

    assert created == [] and len(await svc.list_memories(db_session, learner.id)) == 1


async def test_a_failed_judgement_keeps_both_and_still_advances(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Evenings now.")
    second_id = second.id

    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, "garbage"), conversation_id=second_id
        )

    assert len(await svc.list_memories(db_session, learner.id)) == 2
    conversation = await db_session.get(Conversation, second_id, populate_existing=True)
    assert conversation is not None and conversation.memory_watermark is not None


async def test_an_identical_duplicate_needs_no_judge(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Mornings.")
    llm = _extract_then_judge(MORNINGS, UPDATES_FIRST)

    with _everything_is_equivalent():
        await svc.write_back(db_session, llm, conversation_id=second.id)

    assert _calls(llm) == 1, "only the extraction call"


async def test_summaries_are_never_judged(db_session: AsyncSession) -> None:
    summary = '{"memories": [{"kind": "summary", "content": "Covered vectors."}]}'
    other = '{"memories": [{"kind": "summary", "content": "Covered forces."}]}'
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "vectors")
    await svc.write_back(db_session, fake_llm_client(summary), conversation_id=first.id)
    second = await _conversation(db_session, learner, "forces")
    llm = _extract_then_judge(other, UPDATES_FIRST)

    with _everything_is_equivalent():
        await svc.write_back(db_session, llm, conversation_id=second.id)

    assert _calls(llm) == 1
    assert len(await svc.list_memories(db_session, learner.id)) == 2


async def test_two_candidates_naming_one_neighbour_supersede_it_once(
    db_session: AsyncSession,
) -> None:
    """Review focus 1: a second "updates" for a row already superseded in this run is stored
    as coexisting rather than superseding a non-current row."""
    two = (
        '{"memories": [{"kind": "preference", "content": "Evenings now."}, '
        '{"kind": "preference", "content": "Late evenings, actually."}]}'
    )
    both_update = (
        '{"verdicts": [{"candidate": 1, "verdict": "updates", "replaces": 1}, '
        '{"candidate": 2, "verdict": "updates", "replaces": 1}]}'
    )
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Evenings. No, late evenings.")

    with _everything_is_equivalent():
        await svc.write_back(
            db_session, _extract_then_judge(two, both_update), conversation_id=second.id
        )

    rows = await _memories(db_session, learner.id)
    superseded = [m for m in rows if m.status == MemoryStatus.SUPERSEDED]
    assert len(superseded) == 1
    assert len([m for m in rows if m.status == MemoryStatus.CURRENT]) == 2


# --- forget scope (S42 decision B) ---------------------------------------------------------


async def test_forgetting_a_conversation_suppresses_only_that_conversation(
    db_session: AsyncSession,
) -> None:
    from app.services import removal

    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    first_id = first.id
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first_id)
    await removal.forget_conversation_memories(db_session, learner.id, first_id)

    # The same conversation says it again: still suppressed.
    db_session.add(Message(conversation_id=first_id, role="user", content="Mornings, really."))
    await db_session.commit()
    assert (
        await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first_id) == []
    )

    # Another conversation teaches it: stored again.
    second = await _conversation(db_session, learner, "I study in the mornings.")
    created = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)
    assert [m.content for m in created] == ["Studies in the mornings."]


async def test_a_single_forget_suppresses_everywhere(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [memory] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await svc.delete_memory(db_session, learner.id, memory.id)

    second = await _conversation(db_session, learner, "I study in the mornings.")
    assert (
        await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id) == []
    )


async def test_a_learner_wide_tombstone_wins_over_a_conversation_one(
    db_session: AsyncSession,
) -> None:
    """Review focus 3."""
    from app.services import removal

    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await removal.forget_conversation_memories(db_session, learner.id, first.id)
    second = await _conversation(db_session, learner, "I study in the mornings.")
    [again] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id)
    await svc.delete_memory(db_session, learner.id, again.id)

    third = await _conversation(db_session, learner, "I study in the mornings.")
    assert (
        await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=third.id) == []
    )


async def test_forget_everything_is_learner_wide(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await svc.delete_all_memories(db_session, learner.id)
    rows = await _memories(db_session, learner.id)
    assert {m.forgotten_scope for m in rows} == {"learner"}


async def test_forget_everything_widens_a_conversation_forget(db_session: AsyncSession) -> None:
    from app.services import removal

    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    await removal.forget_conversation_memories(db_session, learner.id, first.id)
    await svc.delete_all_memories(db_session, learner.id)

    second = await _conversation(db_session, learner, "I study in the mornings.")
    assert (
        await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=second.id) == []
    )


# --- undo a replacement --------------------------------------------------------------------


async def _replaced_pair(db_session: AsyncSession, learner: Learner) -> tuple[uuid.UUID, uuid.UUID]:
    """(old id, new id) after "evenings" replaced "mornings"."""
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [old] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    second = await _conversation(db_session, learner, "Evenings now.")
    with _everything_is_equivalent():
        [new] = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, UPDATES_FIRST), conversation_id=second.id
        )
    return old.id, new.id


async def test_undo_restores_the_old_memory_and_retires_the_new(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    old_id, new_id = await _replaced_pair(db_session, learner)

    restored = await svc.undo_replacement(db_session, learner.id, new_id)

    assert restored is not None and restored.id == old_id
    old = await db_session.get(Memory, old_id, populate_existing=True)
    new = await db_session.get(Memory, new_id, populate_existing=True)
    assert old is not None and old.status == MemoryStatus.CURRENT and old.superseded_by_id is None
    assert new is not None and new.status == MemoryStatus.SUPERSEDED
    assert new.superseded_by_id == old_id and new.forgotten_scope is None


async def test_after_undo_the_statement_is_judged_afresh(db_session: AsyncSession) -> None:
    """Review focus 5: undo forgets nothing, so the statement is not suppressed."""
    learner = await _learner(db_session)
    _old_id, new_id = await _replaced_pair(db_session, learner)
    await svc.undo_replacement(db_session, learner.id, new_id)

    third = await _conversation(db_session, learner, "Evenings, as I said.")
    with _everything_is_equivalent():
        created = await svc.write_back(
            db_session, _extract_then_judge(EVENINGS, COEXISTS_FIRST), conversation_id=third.id
        )
    assert [m.content for m in created] == ["Studies in the evenings now."]


async def test_undo_refuses_what_is_not_a_replacement(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [only] = await svc.write_back(db_session, fake_llm_client(MORNINGS), conversation_id=first.id)
    with pytest.raises(svc.NotAReplacement):
        await svc.undo_replacement(db_session, learner.id, only.id)
    assert await svc.undo_replacement(db_session, uuid.uuid4(), only.id) is None


async def test_undo_refuses_once_the_old_memory_has_changed(db_session: AsyncSession) -> None:
    """Review focus 2: the old row was forgotten since — nothing to restore."""
    learner = await _learner(db_session)
    old_id, new_id = await _replaced_pair(db_session, learner)
    old = await db_session.get(Memory, old_id, populate_existing=True)
    assert old is not None
    old.status = MemoryStatus.DELETED
    old.forgotten_scope = "learner"
    await db_session.commit()

    with pytest.raises(svc.NotAReplacement):
        await svc.undo_replacement(db_session, learner.id, new_id)


async def test_undo_reverts_a_correction(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    first = await _conversation(db_session, learner, "I study in the mornings.")
    [original] = await svc.write_back(
        db_session, fake_llm_client(MORNINGS), conversation_id=first.id
    )
    original_id = original.id
    corrected = await svc.correct_memory(
        db_session, fake_llm_client(), learner.id, original_id, content="Studies at night."
    )
    assert corrected is not None

    restored = await svc.undo_replacement(db_session, learner.id, corrected.id)

    assert restored is not None and restored.id == original_id
