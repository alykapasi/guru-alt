"""LLM-generated MCQ items: valid replies persist a real gradable Item; bad ones are skipped."""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation
from app.learning.grading import auto_grade
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from app.models.assessment import ItemType
from app.models.knowledge import KC, Subject, Topic


async def _kc(session: AsyncSession) -> KC:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="photosynthesis", name="Photosynthesis")
    session.add(kc)
    await session.flush()
    return kc


def _client_with_reply(reply: str) -> LLMClient:
    fake = FakeProvider(reply=reply)
    return LLMClient({"fake": fake}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


VALID_REPLY = json.dumps(
    {
        "stem": "What pigment absorbs light for photosynthesis?",
        "choices": ["Chlorophyll", "Melanin", "Keratin", "Collagen"],
        "correct": 0,
    }
)


async def test_generate_mcq_item_persists_gradable_item(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    item, usage = await item_generation.generate_mcq_item(
        db_session, _client_with_reply(VALID_REPLY), kc
    )

    assert item is not None
    assert item.item_type == ItemType.MCQ
    assert item.answer_key == {
        "choices": ["Chlorophyll", "Melanin", "Keratin", "Collagen"],
        "correct": 0,
    }
    assert [link.kc_id for link in item.kc_links] == [kc.id]
    assert usage.output_tokens > 0

    # Round-trips through the real grading path.
    assert item.answer_key is not None
    result = auto_grade(ItemType.MCQ, item.answer_key, {"choice": 0})
    assert result.correct is True
    result = auto_grade(ItemType.MCQ, item.answer_key, {"choice": 1})
    assert result.correct is False


async def test_generate_mcq_item_skips_unparseable_reply(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    item, usage = await item_generation.generate_mcq_item(
        db_session, _client_with_reply("not json at all"), kc
    )
    assert item is None
    assert usage.output_tokens > 0  # the call still happened and cost tokens


async def test_generate_mcq_item_skips_out_of_range_correct_index(
    db_session: AsyncSession,
) -> None:
    kc = await _kc(db_session)
    bad_reply = json.dumps({"stem": "Q?", "choices": ["A", "B"], "correct": 5})
    item, _ = await item_generation.generate_mcq_item(db_session, _client_with_reply(bad_reply), kc)
    assert item is None
