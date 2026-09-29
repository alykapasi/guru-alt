"""Transfer: an unaided answer in a setting the component was never practised in (S14)."""

import json
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation, transfer
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner

SHORT_REPLY = json.dumps({"stem": "A shop sells...", "criteria": ["a", "b"]})


def _client(reply: str) -> tuple[LLMClient, FakeProvider]:
    provider = FakeProvider(reply=reply)
    specs = {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    return LLMClient({"fake": provider}, specs), provider


def test_the_catalogue_starts_abstract_and_has_no_duplicates() -> None:
    names = [name for name, _ in transfer.SETTINGS]
    assert names[0] == transfer.ABSTRACT and len(names) == len(set(names)) == 12


def test_no_setting_is_abstract() -> None:
    assert transfer.setting_of(None) == transfer.ABSTRACT
    assert transfer.setting_of("money") == "money"


def test_the_next_setting_skips_abstract_and_what_was_practised() -> None:
    assert transfer.next_setting({"abstract"}) == "everyday"
    assert transfer.next_setting({"abstract", "everyday"}) == "money"
    assert transfer.next_setting(transfer.NAMES) is None


async def _kc(session: AsyncSession) -> tuple[Learner, KC]:
    learner = Learner(handle=f"tr-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:6]}", name="Percentages")
    session.add(kc)
    await session.flush()
    return learner, kc


async def test_a_setting_reaches_the_prompt_and_the_item(db_session) -> None:
    learner, kc = await _kc(db_session)
    llm, provider = _client(SHORT_REPLY)
    item, _ = await item_generation.generate_short_item(
        db_session, llm, kc, owner_learner_id=learner.id, setting="money"
    )
    assert item is not None and item.setting == "money"
    [(system, _messages)] = provider.prompts_sent
    assert system is not None and "Set the question in this setting: money" in system


async def test_no_setting_leaves_prompt_and_item_as_before(db_session) -> None:
    learner, kc = await _kc(db_session)
    llm, provider = _client(SHORT_REPLY)
    item, _ = await item_generation.generate_short_item(
        db_session, llm, kc, owner_learner_id=learner.id
    )
    assert item is not None and item.setting is None
    [(system, _messages)] = provider.prompts_sent
    assert system is not None and "setting" not in system
