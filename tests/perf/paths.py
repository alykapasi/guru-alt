"""Each hot path once, as the call its endpoint makes (S62).

The budget tests and ``poe perf-report`` run exactly these, so a budget and a timing always
describe the same work. Model calls go to the fake client; what is measured is ours.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.services import analytics, lesson_plan, memory, notes, profile, session_runner, workflow
from app.services import chat as chat_svc
from app.services import sources as sources_svc
from tests.history import SeededHistory

PRESENT = "Here's a worked example. Now try: explain it in your own words."
RIGHT_GRADE = '{"score": 0.9, "rationale": "good"}'
RESPOND = "Great job, you've got it!"
MEMORY_PAGE = 50
"""The memory page the API serves by default (``GET /memory``'s ``limit``)."""


async def turn(session: AsyncSession, h: SeededHistory) -> object:
    settings = get_settings()
    conversation = await chat_svc.get_conversation(
        session, h.conversation_id, learner_id=h.learner_id
    )
    assert conversation is not None
    history = await chat_svc.recent_messages(
        session, h.conversation_id, limit=settings.chat_history_max_messages
    )
    return [
        e
        async for e in chat_svc.run_tutor_turn(
            session,
            fake_llm_client(),
            learner_id=h.learner_id,
            conversation=conversation,
            history=history,
            user_content="Can you explain that again?",
            max_tokens=settings.chat_max_tokens,
        )
    ]


async def practice(session: AsyncSession, h: SeededHistory) -> object:
    """Pose a question and answer it: both halves of a practice round."""
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND)]
    )
    conversation = await session.get(Conversation, h.practice_conversation_id)
    assert conversation is not None
    events = []
    for content, resume in (("let's practice", False), ("my answer", True)):
        events += [
            e
            async for e in workflow.run_workflow_turn(
                session,
                llm,
                learner_id=h.learner_id,
                conversation=conversation,
                user_content=content,
                max_tokens=300,
                max_rounds=3,
                resume=resume,
            )
        ]
    return events


async def conversation_list(session: AsyncSession, h: SeededHistory) -> object:
    return await chat_svc.list_conversations(
        session, h.learner_id, limit=get_settings().chat_conversation_page_size
    )


async def transcript(session: AsyncSession, h: SeededHistory) -> object:
    size = get_settings().chat_transcript_page_size
    page, has_more = await chat_svc.list_messages(session, h.conversation_id, limit=size)
    if has_more and page:
        await chat_svc.list_messages(session, h.conversation_id, limit=size, before=page[0].id)
    return page


async def source_list(session: AsyncSession, h: SeededHistory) -> object:
    return await sources_svc.list_sources(
        session, h.learner_id, limit=get_settings().sources_page_size
    )


async def reviews_due(session: AsyncSession, h: SeededHistory) -> object:
    return await session_runner.due_review_items(
        session,
        fake_llm_client(),
        learner_id=h.learner_id,
        item_limit=get_settings().reviews_due_item_limit,
    )


async def activity(session: AsyncSession, h: SeededHistory) -> object:
    return await analytics.get_activity(session, h.learner_id)


async def subject_mastery(session: AsyncSession, h: SeededHistory) -> object:
    return await analytics.subject_mastery(session, h.learner_id, h.subject_id)


async def memory_list(session: AsyncSession, h: SeededHistory) -> object:
    return await memory.list_memories(session, h.learner_id, limit=MEMORY_PAGE)


async def lesson_plan_read(session: AsyncSession, h: SeededHistory) -> object:
    return await lesson_plan.get_lesson_plan(session, h.learner_id, h.subject_id)


async def plan_revision(session: AsyncSession, h: SeededHistory) -> object:
    return await lesson_plan.revise_plan(session, learner_id=h.learner_id, subject_id=h.subject_id)


async def notes_index(session: AsyncSession, h: SeededHistory) -> object:
    return await notes.notes_index(session, h.learner_id, h.subject_id)


async def profile_refresh(session: AsyncSession, h: SeededHistory) -> object:
    return await profile.refresh_profile(session, h.learner_id, fake_llm_client(), force=True)


PATHS: dict[str, Callable[[AsyncSession, SeededHistory], Awaitable[object]]] = {
    "turn": turn,
    "practice": practice,
    "conversation_list": conversation_list,
    "transcript": transcript,
    "source_list": source_list,
    "reviews_due": reviews_due,
    "activity": activity,
    "subject_mastery": subject_mastery,
    "memory_list": memory_list,
    "lesson_plan": lesson_plan_read,
    "plan_revision": plan_revision,
    "notes_index": notes_index,
    "profile_refresh": profile_refresh,
}
