"""Explicit learner preferences: catalog, storage and resolution (S02, V09)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.preference import LearnerPreference
from app.services import preferences


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"p-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _subject(session: AsyncSession, learner: Learner) -> uuid.UUID:
    tag = uuid.uuid4().hex[:6]
    subject = Subject(slug=f"s-{tag}", name=f"S-{tag}", owner_learner_id=learner.id)
    session.add(subject)
    await session.flush()
    return subject.id


async def test_nothing_set_resolves_to_the_defaults(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    got = await preferences.effective(db_session, learner.id, None)
    assert {k: (r.value, r.source) for k, r in got.items()} == {
        "guidance": ("guided", "default"),
        "explanation_level": ("auto", "default"),
        "note_format": ("auto", "default"),
        "hints": ("auto", "default"),
        "pace": ("auto", "default"),
    }


async def test_subject_beats_global_beats_default(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    await preferences.set_preference(
        db_session, learner.id, "pace", "unhurried", subject_id=subject_id
    )

    assert (await preferences.effective(db_session, learner.id, subject_id))["pace"] == (
        preferences.Resolved("unhurried", "subject")
    )
    assert (await preferences.effective(db_session, learner.id, None))["pace"] == (
        preferences.Resolved("brisk", "global")
    )
    other = await _subject(db_session, learner)
    assert (await preferences.effective(db_session, learner.id, other))["pace"] == (
        preferences.Resolved("brisk", "global")
    )


async def test_an_equal_override_stays_pinned_when_the_default_moves(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(db_session, learner.id, "hints", "more", subject_id=None)
    await preferences.set_preference(db_session, learner.id, "hints", "more", subject_id=subject_id)
    await preferences.set_preference(db_session, learner.id, "hints", "fewer", subject_id=None)

    got = await preferences.effective(db_session, learner.id, subject_id)
    assert got["hints"] == preferences.Resolved("more", "subject"), "an override is pinned"


async def test_a_subject_can_pin_the_default_against_a_different_global(
    db_session: AsyncSession,
) -> None:
    """Global exploration, but guided for this one subject — and "adapt to me" for one subject
    while the global pins hints. Before S02 a subject could choose guided whatever else did."""
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(
        db_session, learner.id, "guidance", "exploration", subject_id=None
    )
    await preferences.set_preference(db_session, learner.id, "hints", "fewer", subject_id=None)

    await preferences.set_preference(
        db_session, learner.id, "guidance", "guided", subject_id=subject_id
    )
    await preferences.set_preference(db_session, learner.id, "hints", "auto", subject_id=subject_id)

    got = await preferences.effective(db_session, learner.id, subject_id)
    assert got["guidance"] == preferences.Resolved("guided", "subject")
    assert got["hints"] == preferences.Resolved("auto", "subject")


async def test_clearing_a_level_removes_only_that_level(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    await preferences.set_preference(
        db_session, learner.id, "pace", "unhurried", subject_id=subject_id
    )

    await preferences.set_preference(db_session, learner.id, "pace", None, subject_id=subject_id)
    assert (await preferences.effective(db_session, learner.id, subject_id))["pace"] == (
        preferences.Resolved("brisk", "global")
    )

    await preferences.set_preference(db_session, learner.id, "pace", None, subject_id=None)
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert rows == []


async def test_the_global_default_value_needs_no_row(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    await preferences.set_preference(db_session, learner.id, "pace", "auto", subject_id=None)
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert rows == []


async def test_setting_twice_updates_the_one_row(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    await preferences.set_preference(db_session, learner.id, "pace", "unhurried", subject_id=None)
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert [r.value for r in rows] == ["unhurried"]


async def test_an_unknown_key_or_value_is_refused(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    with pytest.raises(preferences.InvalidPreference):
        await preferences.set_preference(db_session, learner.id, "font", "big", subject_id=None)
    with pytest.raises(preferences.InvalidPreference):
        await preferences.set_preference(db_session, learner.id, "pace", "warp", subject_id=None)


async def test_a_stored_value_that_left_the_catalog_is_ignored(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    db_session.add_all(
        [
            LearnerPreference(learner_id=learner.id, subject_id=None, key="pace", value="warp"),
            LearnerPreference(learner_id=learner.id, subject_id=None, key="font", value="big"),
        ]
    )
    await db_session.flush()
    got = await preferences.effective(db_session, learner.id, None)
    assert got["pace"] == preferences.Resolved("auto", "default")
    assert "font" not in got


async def test_deleting_a_subject_takes_its_overrides(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=subject_id)
    subject = await db_session.get(Subject, subject_id)
    await db_session.delete(subject)
    await db_session.commit()
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert rows == []


async def test_values_for_never_raises(db_session: AsyncSession, monkeypatch) -> None:
    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(preferences, "effective", broken)
    got = await preferences.values_for(db_session, uuid.uuid4(), None)
    assert got["guidance"] == "guided" and got["pace"] == "auto"


async def test_preferences_are_exported(db_session: AsyncSession) -> None:
    from app.services import retention

    learner = await _learner(db_session)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    exported = await retention.export_learner(db_session, learner.id)
    assert [(p["key"], p["value"]) for p in exported["preferences"]] == [("pace", "brisk")]
