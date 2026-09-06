"""Notes orchestration: staleness, catch-up refresh, absorb, restore, format cascade."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import note_distill
from app.llm import LLMClient
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.note import Note, NoteRevision
from app.models.profile import ProfileDimension
from app.services import notes as notes_svc

ATOMS = [
    {
        "id": "a-1",
        "kind": "concept",
        "kc_ids": [],
        "md": "Vectors add tip-to-tail.",
        "provenance": {},
    }
]
LEARNER_ATOM = {
    "id": "a-L1",
    "kind": "learner",
    "kc_ids": [],
    "md": "My own mnemonic.",
    "provenance": {},
}
RENDERED = "# Vectors\n\nThey add tip-to-tail."


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _distill_then_render(atoms: list[dict]) -> LLMClient:
    """Fake LLM scripted for one refresh: distill reply, then render reply."""
    return fake_llm_client(
        script=[FakeTurn(text=json.dumps({"atoms": atoms})), FakeTurn(text=RENDERED)]
    )


async def _seed(db_session: AsyncSession) -> tuple[Learner, Topic, KC]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="Vectors")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Vector addition")
    db_session.add(kc)
    await db_session.flush()
    return learner, topic, kc


async def _add_observation(db_session: AsyncSession, learner: Learner, kc: KC) -> None:
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=kc.id,
            event_type="observation",
            payload={"score": 0.0, "item_id": None, "response": {"text": "wrong"}, "hints_used": 1},
        )
    )
    await db_session.flush()


async def _add_placement_seed(db_session: AsyncSession, learner: Learner, kc: KC) -> None:
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=kc.id,
            event_type="placement_seed",
            payload={"ability": 0.0, "uncertainty": 1.0, "source": "placement"},
        )
    )
    await db_session.flush()


async def _add_message(db_session: AsyncSession, learner: Learner, topic: Topic) -> None:
    conv = Conversation(learner_id=learner.id, subject_id=topic.subject_id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(
        Message(conversation_id=conv.id, role="assistant", content="Vectors add tip-to-tail.")
    )
    await db_session.flush()


async def test_first_refresh_creates_note_revision_and_render(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    view = await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    assert view.content_md == RENDERED
    assert view.revision_ordinal == 1
    assert view.stale is False
    note = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert note is not None and note.events_watermark > notes_svc.EPOCH


async def test_message_activity_marks_stale(db_session: AsyncSession) -> None:
    learner, topic, _kc = await _seed(db_session)
    await _add_message(db_session, learner, topic)
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.stale is True and view.content_md is None


async def test_no_activity_is_not_stale_and_refresh_is_noop(db_session: AsyncSession) -> None:
    learner, topic, _kc = await _seed(db_session)
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.stale is False
    # refresh with no activity must not call the LLM at all: a script would raise if consumed
    view = await notes_svc.refresh_note(db_session, fake_llm_client(script=[]), learner.id, topic)
    assert view.content_md is None and view.revision_ordinal is None


async def test_placement_seed_alone_is_not_stale_and_refresh_is_noop(
    db_session: AsyncSession,
) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_placement_seed(db_session, learner, kc)
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.stale is False
    # refresh with only placement_seed activity must not call the LLM: a script would raise if consumed
    view = await notes_svc.refresh_note(db_session, fake_llm_client(script=[]), learner.id, topic)
    assert view.content_md is None and view.revision_ordinal is None
    # sanity: an observation event on the same topic still marks it stale (filter isn't over-broad)
    await _add_observation(db_session, learner, kc)
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.stale is True


async def test_no_change_advances_watermark_without_revision(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    llm = fake_llm_client('{"no_change": true}')
    view = await notes_svc.refresh_note(db_session, llm, learner.id, topic)
    assert view.stale is False  # watermark advanced past the event
    note = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert note is not None and note.revision_ordinal == 0
    assert note.events_watermark > notes_svc.EPOCH


async def test_distill_failure_keeps_substrate_and_watermark(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    view = await notes_svc.refresh_note(db_session, fake_llm_client("garbage"), learner.id, topic)
    assert view.stale is True  # nothing advanced; retried on next refresh
    assert await notes_svc.get_note(db_session, learner.id, topic.id) is None


async def test_learner_atom_survives_or_merge_rejected(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    note = Note(
        learner_id=learner.id,
        topic_id=topic.id,
        substrate=[*ATOMS, LEARNER_ATOM],
        revision_ordinal=1,
        messages_watermark=notes_svc.EPOCH,
        events_watermark=notes_svc.EPOCH,
    )
    db_session.add(note)
    await (
        db_session.flush()
    )  # assigns note.id (Note.id default is applied at flush, not construction)
    db_session.add(
        NoteRevision(note_id=note.id, ordinal=1, substrate=note.substrate, cause="distill")
    )
    await db_session.flush()
    await _add_observation(db_session, learner, kc)
    # distill reply drops the learner atom -> merge rejected, substrate intact
    view = await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    assert view.stale is True
    refreshed = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert refreshed is not None
    assert any(a["id"] == "a-L1" for a in refreshed.substrate)


async def test_absorb_edit_creates_learner_edit_revision(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    edited_atoms = [*ATOMS, LEARNER_ATOM]
    llm = fake_llm_client(
        script=[FakeTurn(text=json.dumps({"atoms": edited_atoms})), FakeTurn(text=RENDERED)]
    )
    view = await notes_svc.absorb_edit(db_session, llm, learner.id, topic, "my edited note")
    assert view is not None and view.revision_ordinal == 2
    revs = await notes_svc.list_revisions(db_session, learner.id, topic)
    assert [r.cause for r in revs] == ["distill", "learner_edit"]


async def test_restore_copies_forward_as_new_revision(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    llm = fake_llm_client(
        script=[
            FakeTurn(text=json.dumps({"atoms": [*ATOMS, LEARNER_ATOM]})),
            FakeTurn(text=RENDERED),
        ]
    )
    await notes_svc.absorb_edit(db_session, llm, learner.id, topic, "edit")
    view = await notes_svc.restore_revision(
        db_session, fake_llm_client(script=[FakeTurn(text=RENDERED)]), learner.id, topic, 1
    )
    assert view is not None and view.revision_ordinal == 3
    note = await notes_svc.get_note(db_session, learner.id, topic.id)
    assert note is not None and not any(a["kind"] == "learner" for a in note.substrate)


async def test_revision_source_is_mechanical(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    src = await notes_svc.revision_source(db_session, learner.id, topic, 1)
    assert src is not None and "## Concepts" in src


async def test_format_cascade(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    # fallback
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.effective_format == note_distill.FALLBACK_FORMAT
    # learned dimension wins over fallback
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="note_format",
            value="narrative",
            uncertainty=0.2,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.effective_format == "narrative"
    # explicit choice wins over everything; set_format also renders the new format
    await _add_observation(db_session, learner, kc)
    await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)
    llm = fake_llm_client(script=[FakeTurn(text="mnemonic render")])
    view = await notes_svc.set_format(db_session, llm, learner.id, topic, "mnemonic")
    assert view.effective_format == "mnemonic" and view.content_md == "mnemonic render"


async def test_conceptual_error_share_biases_worked_examples(db_session: AsyncSession) -> None:
    learner, topic, _kc = await _seed(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="error_type",
            value={"conceptual": 0.6, "procedural": 0.3, "careless": 0.1},
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()
    view = await notes_svc.note_view(db_session, learner.id, topic)
    assert view.effective_format == "worked_examples"


async def test_notes_index(db_session: AsyncSession) -> None:
    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)
    entries = await notes_svc.notes_index(db_session, learner.id, topic.subject_id)
    assert len(entries) == 1
    assert entries[0]["topic_id"] == topic.id
    assert entries[0]["has_note"] is False and entries[0]["stale"] is True


# --- S38: per-stream catch-up cursors ---------------------------------------
#
# created_at is server_default=func.now(), i.e. transaction-start time, so rows written
# together share a timestamp. These tests set it explicitly to control ordering.


async def _msg_at(db_session: AsyncSession, learner: Learner, topic: Topic, at: datetime) -> None:
    conv = Conversation(learner_id=learner.id, subject_id=topic.subject_id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(Message(conversation_id=conv.id, role="user", content="q", created_at=at))
    await db_session.flush()


async def _event_at(db_session: AsyncSession, learner: Learner, kc: KC, at: datetime) -> None:
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=kc.id,
            event_type="observation",
            payload={"score": 1.0, "item_id": None, "response": {"text": "x"}, "hints_used": 0},
            created_at=at,
        )
    )
    await db_session.flush()


async def test_gather_does_not_skip_messages_when_events_run_ahead(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The bug: one watermark advanced to the max across BOTH streams.

    With messages truncated by their limit and an event timestamped later, the shared
    watermark jumped past the unread messages, skipping them permanently.
    """
    learner, topic, kc = await _seed(db_session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(3):  # 3 messages, limit will be 2
        await _msg_at(db_session, learner, topic, base + timedelta(minutes=i))
    await _event_at(db_session, learner, kc, base + timedelta(hours=1))  # later than every message

    monkeypatch.setattr(get_settings(), "note_distill_max_messages", 2)
    monkeypatch.setattr(get_settings(), "note_distill_max_outcome_events", 50)
    gathered = await notes_svc._gather(
        db_session, learner.id, topic, notes_svc.EPOCH, notes_svc.EPOCH
    )

    # the third message is still pending: its cursor must not pass it
    assert gathered.messages_watermark < base + timedelta(minutes=2)
    # the event stream, fully consumed, advances independently
    assert gathered.events_watermark == base + timedelta(hours=1)


async def test_gather_leaves_a_split_timestamp_group_for_the_next_pass(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A multi-KC answer writes several events with one timestamp; a page must not split it."""
    learner, topic, kc = await _seed(db_session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    await _event_at(db_session, learner, kc, base)
    await _event_at(db_session, learner, kc, base + timedelta(minutes=5))  # tied pair
    await _event_at(db_session, learner, kc, base + timedelta(minutes=5))

    monkeypatch.setattr(get_settings(), "note_distill_max_outcome_events", 2)
    gathered = await notes_svc._gather(
        db_session, learner.id, topic, notes_svc.EPOCH, notes_svc.EPOCH
    )

    # stops below the tied group so both of its rows are read together next time
    assert gathered.events_watermark == base


async def test_gather_advances_both_cursors_when_nothing_is_truncated(
    db_session: AsyncSession,
) -> None:
    learner, topic, kc = await _seed(db_session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    await _msg_at(db_session, learner, topic, base)
    await _event_at(db_session, learner, kc, base + timedelta(minutes=1))

    gathered = await notes_svc._gather(
        db_session, learner.id, topic, notes_svc.EPOCH, notes_svc.EPOCH
    )

    assert gathered.messages_watermark == base
    assert gathered.events_watermark == base + timedelta(minutes=1)


async def test_refresh_stays_stale_while_truncated_activity_remains(
    db_session: AsyncSession, monkeypatch
) -> None:
    """End-to-end: a backlog bigger than one page leaves the note stale, not silently done."""
    learner, topic, kc = await _seed(db_session)
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(3):
        await _msg_at(db_session, learner, topic, base + timedelta(minutes=i))
    await _event_at(db_session, learner, kc, base + timedelta(hours=1))

    monkeypatch.setattr(get_settings(), "note_distill_max_messages", 2)
    view = await notes_svc.refresh_note(db_session, _distill_then_render(ATOMS), learner.id, topic)

    assert view.stale is True  # the unread third message is still waiting
