"""Who a model call is for and what it is for travels with the work, not through 29 call sites."""

import uuid

from app.llm.attribution import attributed, current, metered


def test_nesting_overrides_only_what_it_names() -> None:
    learner = uuid.uuid4()
    with attributed(learner_id=learner, feature="chat_turn", request_id="r1"):
        with attributed(feature="practice_grading"):
            inner = current()
        outer = current()
    assert (inner.learner_id, inner.feature, inner.request_id) == (
        learner,
        "practice_grading",
        "r1",
    )
    assert outer.feature == "chat_turn"
    assert current().feature is None


async def test_metered_reads_ids_from_the_arguments() -> None:
    learner, conversation_id = uuid.uuid4(), uuid.uuid4()

    class Conversation:
        id = conversation_id

    @metered("chat_turn", learner="learner_id", conversation="conversation.id")
    async def turn(*, learner_id: uuid.UUID, conversation: object) -> tuple:
        seen = current()
        return seen.feature, seen.learner_id, seen.conversation_id

    assert await turn(learner_id=learner, conversation=Conversation()) == (
        "chat_turn",
        learner,
        conversation_id,
    )
    assert current().feature is None


async def test_metered_wraps_an_async_generator() -> None:
    @metered("goal_refinement")
    async def events():
        yield current().feature
        yield current().feature

    assert [e async for e in events()] == ["goal_refinement", "goal_refinement"]
    assert current().feature is None


async def test_each_request_records_its_own_learner(
    api_client, anon_client, db_session, api_learner
) -> None:
    """``bind`` in the auth dependency must not leak from one request into the next."""
    from sqlalchemy import select

    from app.api.deps import get_llm_client
    from app.llm.registry import fake_llm_client
    from app.main import app
    from app.models.chat import LLMCall
    from app.models.learner import Learner
    from tests.conftest import sign_in

    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client()

    other = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    await sign_in(anon_client, db_session, other)

    for client in (api_client, anon_client, api_client):
        r = await client.post("/api/v1/retrieve", json={"query": "mitochondria"})
        assert r.status_code == 200

    rows = (
        await db_session.execute(
            select(LLMCall.learner_id, LLMCall.feature, LLMCall.request_id).order_by(
                LLMCall.created_at
            )
        )
    ).all()
    me = api_learner.id
    assert [r.learner_id for r in rows] == [me, other.id, me]
    assert {r.feature for r in rows} == {"retrieval"}
    assert len({r.request_id for r in rows}) == 3
