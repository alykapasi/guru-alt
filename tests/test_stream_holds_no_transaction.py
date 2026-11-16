"""No streamed reply waits on the model with the request's transaction open.

A transaction holds its pool connection until it ends, and a reply can take until the turn
deadline. Every flow reads (and sometimes writes) before and between its model calls, so each
one must end its transaction before a stream starts — or every learner waiting on a reply
holds a connection the rest of the app is waiting on too.

The provider below records, at the start of each stream, whether the request session was
inside a transaction. Every recorded value must be False.
"""

import uuid
from collections.abc import AsyncIterator, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.providers import FakeProvider
from app.llm.providers.fake import FakeTurn
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatChunk, ChatMessage, ModelRole, ToolCall, ToolDef
from app.models.chat import Conversation
from app.models.learner import Learner
from app.services.agentic import run_agentic_turn
from app.services.chat import run_tutor_turn
from app.services.refinement import run_refinement_turn
from app.services.workflow import run_workflow_turn
from tests.test_workflow import PRESENT, RESPOND_2, RIGHT_GRADE, _conversation_with_active_step


class WatchingProvider(FakeProvider):
    """A fake that notes whether ``session`` held a transaction as each stream began."""

    def __init__(self, session: AsyncSession, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session
        self.in_transaction_at_stream: list[bool] = []

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        self.in_transaction_at_stream.append(self._session.in_transaction())
        async for chunk in super().stream(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        ):
            yield chunk


def _client(provider: FakeProvider) -> LLMClient:
    roles = {role: ModelSpec(provider="fake", model="fake-1") for role in ModelRole}
    return LLMClient({"fake": provider}, roles)


async def _plain_conversation(session: AsyncSession) -> Conversation:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    return conversation


async def test_a_tutor_reply_streams_outside_a_transaction(db_session: AsyncSession) -> None:
    # A subject with an active step: the turn reads context and poses a check before replying.
    conv = await _conversation_with_active_step(db_session)
    provider = WatchingProvider(db_session)

    events = [
        ev
        async for ev in run_tutor_turn(
            db_session,
            _client(provider),
            learner_id=conv.learner_id,
            conversation=conv,
            history=[],
            user_content="What is photosynthesis?",
            max_tokens=256,
        )
    ]

    assert any(e.type == "done" for e in events)
    assert provider.in_transaction_at_stream == [False]


async def test_guided_practice_streams_outside_a_transaction(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    # present (stream), then on resume: grade (complete), respond (stream).
    provider = WatchingProvider(
        db_session,
        script=[FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)],
    )
    llm = _client(provider)

    async def turn(content: str, *, resume: bool) -> list:
        return [
            ev
            async for ev in run_workflow_turn(
                db_session,
                llm,
                learner_id=conv.learner_id,
                conversation=conv,
                user_content=content,
                max_tokens=256,
                max_rounds=3,
                resume=resume,
            )
        ]

    await turn("let's practice", resume=False)
    # The resumed turn grades (reads and commits) and then reads again before it replies.
    await turn("Plants turn light into sugar.", resume=True)

    assert provider.in_transaction_at_stream == [False, False]


async def test_an_agentic_reply_streams_outside_a_transaction_after_a_search(
    db_session: AsyncSession,
) -> None:
    # A subject gives the search a scope, so the tool really reads the database mid-turn.
    conv = await _conversation_with_active_step(db_session)
    provider = WatchingProvider(
        db_session,
        script=[
            FakeTurn(
                tool_calls=[ToolCall(id="t1", name="search_materials", input={"query": "light"})]
            ),
            FakeTurn(text="Here is what your notes say."),
        ],
    )

    events = [
        ev
        async for ev in run_agentic_turn(
            db_session,
            _client(provider),
            learner_id=conv.learner_id,
            conversation=conv,
            history=[],
            user_content="What do my notes say about light?",
            max_tokens=256,
        )
    ]

    assert any(e.type == "tool_call" for e in events)
    assert provider.in_transaction_at_stream == [False, False]


async def test_a_refinement_proposal_streams_outside_a_transaction(
    db_session: AsyncSession,
) -> None:
    conv = await _plain_conversation(db_session)
    # The route reads the conversation before the flow runs; this stands in for that read.
    await db_session.execute(select(Conversation).where(Conversation.id == conv.id))
    provider = WatchingProvider(db_session, reply="Shall we aim to understand derivatives?")

    events = [
        ev
        async for ev in run_refinement_turn(
            db_session,
            _client(provider),
            learner_id=conv.learner_id,
            conversation=conv,
            user_content="I want to learn calculus",
            satisfied=False,
            max_tokens=256,
            max_rounds=3,
            resume=False,
            persist_user=False,
        )
    ]

    assert any(e.type == "awaiting_reply" for e in events)
    assert provider.in_transaction_at_stream == [False]
