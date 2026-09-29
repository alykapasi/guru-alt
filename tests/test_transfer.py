"""Transfer: an unaided answer in a setting the component was never practised in (S14)."""

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation, mastery, transfer
from app.learning.mastery import Observation
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from app.models.assessment import Item, ItemKC, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from tests.test_item_exposure import T0, _learner
from tests.test_item_exposure import _kc as _exposure_kc

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


# --- what counts as transfer ------------------------------------------------------------------


async def _item_in(session: AsyncSession, kc: KC, setting: str | None) -> Item:
    item = Item(
        visibility="curated",
        item_type=ItemType.MCQ,
        stem=f"q-{uuid.uuid4().hex[:6]}",
        difficulty=0.0,
        answer_key={"correct": 0},
        setting=setting,
    )
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id, weight=1.0))
    await session.flush()
    return item


async def _answer(
    session, learner, kc, item, *, when, correct=True, hints=None, taught_first=False
) -> None:
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0 if correct else 0.0,
            correct=correct,
            item_id=item.id,
            hints_used=hints,
            taught_first=taught_first,
        ),
        now=when,
    )
    await session.flush()


async def _evidence(session, learner, kc) -> mastery.KCEvidence:
    (ev,) = (await mastery.kc_evidence(session, learner.id, [kc.id])).values()
    return ev


async def test_a_correct_unaided_answer_in_a_new_setting_is_transfer(db_session) -> None:
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, None), when=T0)
    money = await _item_in(db_session, kc, "money")
    await _answer(db_session, learner, kc, money, when=T0 + timedelta(days=3))
    ev = await _evidence(db_session, learner, kc)
    assert ev.transfer_shown and ev.transfer_setting == "money"
    assert ev.practised_settings == frozenset({"abstract", "money"})


async def test_a_first_answer_is_not_transfer(db_session) -> None:
    """Review focus 1."""
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, "money"), when=T0)
    assert not (await _evidence(db_session, learner, kc)).transfer_shown


@pytest.mark.parametrize("variant", ["practised", "wrong", "hinted", "taught_first"])
async def test_what_is_not_transfer(db_session, variant: str) -> None:
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    first = "money" if variant == "practised" else None
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, first), when=T0)
    await _answer(
        db_session,
        learner,
        kc,
        await _item_in(db_session, kc, "money"),
        when=T0 + timedelta(days=3),
        correct=variant != "wrong",
        hints=1 if variant == "hinted" else None,
        taught_first=variant == "taught_first",
    )
    assert not (await _evidence(db_session, learner, kc)).transfer_shown


async def test_a_deleted_item_counts_as_abstract(db_session) -> None:
    """Review focus 2."""
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    old = await _item_in(db_session, kc, None)
    await _answer(db_session, learner, kc, old, when=T0)
    await db_session.execute(delete(Item).where(Item.id == old.id))
    await db_session.flush()
    money = await _item_in(db_session, kc, "money")
    await _answer(db_session, learner, kc, money, when=T0 + timedelta(days=3))
    ev = await _evidence(db_session, learner, kc)
    assert ev.transfer_setting == "money" and "abstract" in ev.practised_settings


# --- when a transfer check is due ---------------------------------------------------------------


async def _retained(session: AsyncSession) -> tuple[Learner, KC]:
    """Retention shown: two unaided abstract answers a week apart."""
    learner = await _learner(session)
    _s, kc = await _exposure_kc(session)
    await _answer(session, learner, kc, await _item_in(session, kc, None), when=T0)
    later = await _item_in(session, kc, None)
    await _answer(session, learner, kc, later, when=T0 + timedelta(days=7))
    return learner, kc


async def _due(session, learner, now) -> list[uuid.UUID]:
    return [c.kc_id for c in await mastery.due_transfer_checks(session, learner.id, now=now)]


async def test_a_transfer_check_is_due_after_retention(db_session) -> None:
    learner, kc = await _retained(db_session)
    assert await _due(db_session, learner, T0 + timedelta(days=7, hours=2)) == []
    [check] = await mastery.due_transfer_checks(db_session, learner.id, now=T0 + timedelta(days=9))
    assert check.kc_id == kc.id and check.due_at.utcoffset() == timedelta(0)


async def test_no_check_before_retention(db_session) -> None:
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, None), when=T0)
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_no_check_once_transfer_is_shown(db_session) -> None:
    learner, kc = await _retained(db_session)
    money = await _item_in(db_session, kc, "money")
    await _answer(db_session, learner, kc, money, when=T0 + timedelta(days=9))
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_no_check_when_every_setting_is_practised(db_session) -> None:
    """Review focus 4."""
    learner, kc = await _retained(db_session)
    names = [n for n, _ in transfer.SETTINGS if n != transfer.ABSTRACT]
    for i, name in enumerate(names):
        item = await _item_in(db_session, kc, name)
        await _answer(
            db_session, learner, kc, item, when=T0 + timedelta(days=8, minutes=i), correct=False
        )
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_a_failed_check_moves_to_the_next_setting(db_session) -> None:
    learner, kc = await _retained(db_session)
    everyday = await _item_in(db_session, kc, "everyday")
    await _answer(db_session, learner, kc, everyday, when=T0 + timedelta(days=9), correct=False)
    ev = await _evidence(db_session, learner, kc)
    assert transfer.next_setting(ev.practised_settings) == "money"
    assert await _due(db_session, learner, T0 + timedelta(days=9, hours=12)) == []
    assert await _due(db_session, learner, T0 + timedelta(days=10, hours=1)) == [kc.id]
