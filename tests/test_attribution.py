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
