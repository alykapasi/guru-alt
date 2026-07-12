"""LLM-generated items: valid replies persist a real gradable Item; bad ones are skipped."""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation
from app.learning.grading import auto_grade, grade_flashcard
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


FILL_BLANK_REPLY = json.dumps(
    {"stem": "The powerhouse of the cell is the ___.", "answer": "mitochondria"}
)


async def test_generate_fill_blank_item_persists_gradable_item(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    item, usage = await item_generation.generate_fill_blank_item(
        db_session, _client_with_reply(FILL_BLANK_REPLY), kc
    )

    assert item is not None
    assert item.item_type == ItemType.FILL_BLANK
    assert item.answer_key == {"blanks": ["mitochondria"]}
    assert [link.kc_id for link in item.kc_links] == [kc.id]
    assert usage.output_tokens > 0

    assert item.answer_key is not None
    result = auto_grade(ItemType.FILL_BLANK, item.answer_key, {"blanks": ["mitochondria"]})
    assert result.correct is True
    result = auto_grade(ItemType.FILL_BLANK, item.answer_key, {"blanks": ["wrong"]})
    assert result.correct is False


async def test_generate_fill_blank_item_skips_unparseable_reply(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    item, usage = await item_generation.generate_fill_blank_item(
        db_session, _client_with_reply("not json at all"), kc
    )
    assert item is None
    assert usage.output_tokens > 0


async def test_generate_fill_blank_item_skips_missing_blank_marker(
    db_session: AsyncSession,
) -> None:
    kc = await _kc(db_session)
    bad_reply = json.dumps(
        {"stem": "The powerhouse of the cell is what?", "answer": "mitochondria"}
    )
    item, _ = await item_generation.generate_fill_blank_item(
        db_session, _client_with_reply(bad_reply), kc
    )
    assert item is None


async def test_generate_fill_blank_item_skips_empty_answer(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    bad_reply = json.dumps({"stem": "The powerhouse of the cell is the ___.", "answer": ""})
    item, _ = await item_generation.generate_fill_blank_item(
        db_session, _client_with_reply(bad_reply), kc
    )
    assert item is None


FLASHCARD_REPLY = json.dumps(
    {"stem": "What is the powerhouse of the cell?", "answer": "The mitochondria"}
)


async def test_generate_flashcard_item_persists_self_graded_item(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    item, usage = await item_generation.generate_flashcard_item(
        db_session, _client_with_reply(FLASHCARD_REPLY), kc
    )

    assert item is not None
    assert item.item_type == ItemType.FLASHCARD
    assert item.answer_key == {"back": "The mitochondria"}
    assert [link.kc_id for link in item.kc_links] == [kc.id]
    assert usage.output_tokens > 0

    # Self-graded — nothing about the stored "back" affects grading; the learner's rating is
    # trusted outright (see the module docstring's documented no-reveal-path gap).
    result = grade_flashcard({"rating": 4})
    assert result.correct is True


async def test_generate_flashcard_item_skips_unparseable_reply(db_session: AsyncSession) -> None:
    kc = await _kc(db_session)
    item, usage = await item_generation.generate_flashcard_item(
        db_session, _client_with_reply("not json at all"), kc
    )
    assert item is None
    assert usage.output_tokens > 0


def test_generators_cover_every_generatable_item_type() -> None:
    assert set(item_generation.GENERATORS) == {
        ItemType.MCQ,
        ItemType.FILL_BLANK,
        ItemType.FLASHCARD,
    }
