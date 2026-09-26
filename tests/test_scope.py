"""One scope rule for every retrieval path (S26, V05)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import NO_SCOPE_RESULT, build_tools
from app.llm.registry import fake_llm_client
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.rag.scope import SourceScope, resolve_scope


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _subject(session: AsyncSession, owner: Learner, **switches: bool) -> Subject:
    subject = Subject(
        slug=f"s-{uuid.uuid4().hex[:8]}", name="S", owner_learner_id=owner.id, **switches
    )
    session.add(subject)
    await session.flush()
    return subject


async def test_a_general_conversation_has_no_scope(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert await resolve_scope(db_session, learner_id=learner.id, subject_id=None) is None


async def test_a_subject_leaves_untagged_sources_out_by_default(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _subject(db_session, learner)

    scope = await resolve_scope(db_session, learner_id=learner.id, subject_id=subject.id)

    assert scope == SourceScope(learner_id=learner.id, subject_id=subject.id)


async def test_a_subject_carries_both_switches(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _subject(db_session, learner, include_untagged_sources=True, sources_only=True)

    scope = await resolve_scope(db_session, learner_id=learner.id, subject_id=subject.id)

    assert scope is not None
    assert scope.include_untagged and scope.sources_only


async def test_picked_sources_override_the_untagged_switch(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _subject(db_session, learner, include_untagged_sources=True)
    picked = uuid.uuid4()

    scope = await resolve_scope(
        db_session, learner_id=learner.id, subject_id=subject.id, source_ids=[picked]
    )

    assert scope is not None
    assert scope.source_ids == (picked,)
    assert scope.include_untagged is False


async def test_sources_without_a_subject_scope_to_exactly_those(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    picked = uuid.uuid4()

    scope = await resolve_scope(
        db_session, learner_id=learner.id, subject_id=None, source_ids=[picked]
    )

    assert scope == SourceScope(learner_id=learner.id, source_ids=(picked,))


async def test_agent_search_in_a_general_chat_reaches_no_materials(
    db_session: AsyncSession,
) -> None:
    """The leak this closes: a chat labelled "no library grounding" searched every source."""
    tools = build_tools(db_session, fake_llm_client(), scope=None)
    search = next(t for t in tools if t.name == "search_materials")

    result = await search.execute({"query": "anything"})

    assert not result.is_error
    assert result.content == NO_SCOPE_RESULT
