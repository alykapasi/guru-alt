"""Notes orchestration: staleness, catch-up distillation, edits, history (Phase 8).

Owns transactions and cost logging; all model-reply handling lives in
``app/learning/note_distill.py``. GET-path functions (``note_view``, ``notes_index``) are
pure reads — the work happens behind explicit refresh/edit calls, avoiding the
"GET with generation side-effects" compromise the reviews-due endpoint had to accept.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import note_distill
from app.learning.note_distill import FALLBACK_FORMAT, FORMATS, NOTES_ROLE
from app.llm import LLMClient
from app.models.assessment import Item
from app.models.chat import Conversation, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learning import LearningEvent
from app.models.note import WATERMARK_EPOCH, Note, NoteRender, NoteRevision
from app.models.profile import ProfileDimension
from app.services.llm_log import log_llm_call

log = structlog.get_logger(__name__)

EPOCH = WATERMARK_EPOCH


@dataclass(frozen=True)
class NoteView:
    """What the API returns for a note in any state (including 'no note yet')."""

    topic_id: uuid.UUID
    content_md: str | None
    format: str | None
    effective_format: str
    stale: bool
    revision_ordinal: int | None
    updated_at: datetime | None


def _now() -> datetime:
    # Naive UTC on purpose: LearningEvent/Message.created_at are tz-naive columns; a tz-aware
    # comparison crashes asyncpg (see app/services/analytics.py). Do not "fix" to tz-aware.
    return datetime.now(UTC).replace(tzinfo=None)


async def get_note(
    session: AsyncSession, learner_id: uuid.UUID, topic_id: uuid.UUID
) -> Note | None:
    return await session.scalar(
        select(Note).where(Note.learner_id == learner_id, Note.topic_id == topic_id)
    )


async def _dimension_value(session: AsyncSession, learner_id: uuid.UUID, key: str) -> object:
    dim = await session.scalar(
        select(ProfileDimension).where(
            ProfileDimension.learner_id == learner_id, ProfileDimension.key == key
        )
    )
    return dim.value if dim is not None else None


async def _format_inputs(session: AsyncSession, learner_id: uuid.UUID) -> tuple[object, object]:
    """The two learner-global profile values the format cascade reads.

    Split out so a caller handling many topics fetches them once rather than once per topic —
    they cannot differ between topics (S62).
    """
    learned = await _dimension_value(session, learner_id, "note_format")
    errors = await _dimension_value(session, learner_id, "error_type")
    return learned, errors.get("conceptual", 0) if isinstance(errors, dict) else None


def _format_from(note: Note | None, learned: object, conceptual: object) -> str:
    """The cascade itself, over values already in hand: explicit choice > learned dimension >
    heuristic > outline."""
    if note is not None and note.format:
        return note.format
    if isinstance(learned, str) and learned in FORMATS:
        return learned
    # Heuristic: a majority-conceptual error profile benefits from example-led notes.
    if isinstance(conceptual, int | float) and conceptual >= 0.5:
        return "worked_examples"
    return FALLBACK_FORMAT


async def effective_format(session: AsyncSession, learner_id: uuid.UUID, note: Note | None) -> str:
    """The cascade: explicit choice > learned note_format dimension > heuristic > outline."""
    if note is not None and note.format:
        return note.format
    return _format_from(note, *await _format_inputs(session, learner_id))


def _cursors(note: Note | None) -> tuple[datetime, datetime]:
    """This note's (messages, events) cursors — EPOCH for a note that does not exist yet."""
    if note is None:
        return EPOCH, EPOCH
    return note.messages_watermark, note.events_watermark


async def _has_new_activity(
    session: AsyncSession,
    learner_id: uuid.UUID,
    topic: Topic,
    messages_watermark: datetime,
    events_watermark: datetime,
) -> bool:
    kc_ids = select(KC.id).where(KC.topic_id == topic.id).scalar_subquery()
    event = await session.scalar(
        select(LearningEvent.id)
        .where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.kc_id.in_(kc_ids),
            LearningEvent.created_at > events_watermark,
            LearningEvent.event_type == "observation",
        )
        .limit(1)
    )
    if event is not None:
        return True
    message = await session.scalar(
        select(Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.learner_id == learner_id,
            Conversation.subject_id == topic.subject_id,
            Message.created_at > messages_watermark,
        )
        .limit(1)
    )
    return message is not None


async def _current_render(session: AsyncSession, note: Note, note_format: str) -> NoteRender | None:
    return await session.scalar(
        select(NoteRender).where(
            NoteRender.note_id == note.id,
            NoteRender.revision_ordinal == note.revision_ordinal,
            NoteRender.format == note_format,
        )
    )


async def _is_stale(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, note: Note | None, fmt: str
) -> bool:
    if await _has_new_activity(session, learner_id, topic, *_cursors(note)):
        return True
    # Render-failure recovery: substrate current but no cached render for the effective format.
    if note is not None and note.revision_ordinal > 0:
        return await _current_render(session, note, fmt) is None
    return False


async def _view(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, note: Note | None
) -> NoteView:
    fmt = await effective_format(session, learner_id, note)
    content = None
    if note is not None and note.revision_ordinal > 0:
        render_row = await _current_render(session, note, fmt)
        content = render_row.content_md if render_row is not None else None
    return NoteView(
        topic_id=topic.id,
        content_md=content,
        format=note.format if note is not None else None,
        effective_format=fmt,
        stale=await _is_stale(session, learner_id, topic, note, fmt),
        revision_ordinal=(
            note.revision_ordinal if note is not None and note.revision_ordinal > 0 else None
        ),
        updated_at=note.updated_at if note is not None else None,
    )


async def note_view(session: AsyncSession, learner_id: uuid.UUID, topic: Topic) -> NoteView:
    """Pure read — never calls a model, never writes."""
    return await _view(session, learner_id, topic, await get_note(session, learner_id, topic.id))


@dataclass(frozen=True)
class _Gathered:
    transcript: str
    outcomes: str
    refs: dict[str, dict]
    """The bracketed labels used in ``transcript``/``outcomes`` -> the durable row behind each,
    so a reference the model cites can be resolved to something that outlives the prompt."""
    messages_watermark: datetime
    events_watermark: datetime


def _advance(rows: Sequence[Any], limit: int, current: datetime) -> tuple[list[Any], datetime]:
    """Trim one fetched page to a safe boundary and return its new cursor.

    Rows arrive ordered by ``created_at`` with one extra row fetched, so ``len(rows) > limit``
    means more activity is waiting. Two rules keep a cursor from stepping over unread rows:

    * A page that ends mid-timestamp drops that trailing group — ``created_at`` is
      transaction-start time, so one answer tagged to several KCs writes several events at the
      identical instant, and a ``> watermark`` cursor landing inside that group would skip its
      remainder forever. The group is left whole for the next pass.
    * The cursor only ever advances to a row this page actually consumed, never to the newest
      row in some *other* stream.
    """
    if len(rows) <= limit:
        return list(rows), max((r.created_at for r in rows), default=current)
    page = list(rows[:limit])
    boundary = page[-1].created_at
    if rows[limit].created_at > boundary:
        return page, boundary  # the page happens to end on a complete group
    trimmed = [r for r in page if r.created_at < boundary]
    if trimmed:
        return trimmed, trimmed[-1].created_at
    log.warning(  # one instant holds more rows than a whole page; taking it splits the group
        "notes.same_timestamp_group_exceeds_page", limit=limit, at=boundary.isoformat()
    )
    return page, boundary


async def _gather(
    session: AsyncSession,
    learner_id: uuid.UUID,
    topic: Topic,
    messages_watermark: datetime,
    events_watermark: datetime,
) -> _Gathered:
    settings = get_settings()
    kcs = (await session.scalars(select(KC).where(KC.topic_id == topic.id))).all()
    kc_names = {kc.id: kc.name for kc in kcs}

    events = (
        await session.scalars(
            select(LearningEvent)
            .where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.kc_id.in_(list(kc_names)),
                LearningEvent.created_at > events_watermark,
                LearningEvent.event_type == "observation",
            )
            .order_by(LearningEvent.created_at)
            .limit(settings.note_distill_max_outcome_events + 1)
        )
    ).all()
    events, new_events_watermark = _advance(
        events, settings.note_distill_max_outcome_events, events_watermark
    )

    item_ids = {uuid.UUID(e.payload["item_id"]) for e in events if e.payload.get("item_id")}
    items: dict[uuid.UUID, Item] = {}
    if item_ids:
        rows = (await session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
        items = {item.id: item for item in rows}

    # Each line is labelled so an atom can cite the evidence it came from, and each label maps
    # back to a durable row — an attempt id here, a message id below.
    refs: dict[str, dict] = {}
    outcome_lines: list[str] = []
    for n, event in enumerate(events, start=1):
        label = f"o{n}"
        refs[label] = {
            "kind": "attempt",
            "id": str(event.attempt_id or event.id),
            "kc_id": str(event.kc_id) if event.kc_id else None,
        }
        kc_name = kc_names.get(event.kc_id, "?")
        line = f"[{label}] KC '{kc_name}': score={event.payload.get('score')}, hints={event.payload.get('hints_used', 0)}"
        item_id = event.payload.get("item_id")
        if item_id and uuid.UUID(item_id) in items:
            line += f"; question: {items[uuid.UUID(item_id)].stem!r}"
        if event.payload.get("response") is not None:
            line += f"; their answer: {event.payload['response']!r}"
        outcome_lines.append(line)

    messages = (
        await session.scalars(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.learner_id == learner_id,
                Conversation.subject_id == topic.subject_id,
                Message.created_at > messages_watermark,
            )
            .order_by(Message.created_at)
            .limit(settings.note_distill_max_messages + 1)
        )
    ).all()
    messages, new_messages_watermark = _advance(
        messages, settings.note_distill_max_messages, messages_watermark
    )
    transcript_lines: list[str] = []
    for n, message in enumerate(messages, start=1):
        label = f"m{n}"
        refs[label] = {"kind": "message", "id": str(message.id)}
        transcript_lines.append(f"[{label}] {message.role}: {message.content}")

    return _Gathered(
        transcript="\n".join(transcript_lines),
        outcomes="\n".join(outcome_lines),
        refs=refs,
        messages_watermark=new_messages_watermark,
        events_watermark=new_events_watermark,
    )


async def _topic_context(session: AsyncSession, topic: Topic) -> note_distill.TopicContext:
    """Name the topic and enumerate its KCs for the merge prompt.

    The transcript spans the whole subject (messages are not topic-tagged), so without this the
    model was asked to keep a note about "this topic" with nothing identifying it.
    """
    subject = await session.get(Subject, topic.subject_id)
    kcs = (await session.scalars(select(KC).where(KC.topic_id == topic.id).order_by(KC.slug))).all()
    return note_distill.TopicContext(
        name=topic.name,
        subject_name=subject.name if subject is not None else "?",
        description=topic.description,
        kcs=tuple((str(kc.id), kc.name) for kc in kcs),
    )


async def _render_and_cache(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, note: Note, fmt: str
) -> NoteRender:
    """Cache a rendering of the current substrate, falling back when the model cannot give one.

    The substrate is the note; a render is a projection of it. So a failed, empty or severed
    render is never a reason for a learner to be shown a blank or half a note — the
    deterministic ``mechanical_render`` of the same atoms is always available and always
    complete. It reads plainer, and it is the whole note.
    """
    content: str | None = None
    try:
        content, usage = await note_distill.render(
            llm,
            atoms=note.substrate,
            note_format=fmt,
        )
        await log_llm_call(
            learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
        )
    except Exception as exc:
        # A provider outage must not cost the learner the revision this render belongs to.
        log.warning("notes.render_failed", note_id=str(note.id), error=str(exc))
    if content is None:
        content = note_distill.mechanical_render(note.substrate)
    render_row = NoteRender(
        note_id=note.id, revision_ordinal=note.revision_ordinal, format=fmt, content_md=content
    )
    session.add(render_row)
    await session.flush()
    return render_row


class RevisionConflict(Exception):
    """The note moved on since the client last read it. Carries the ordinal it is at now."""

    def __init__(self, current: int) -> None:
        super().__init__(f"note is at revision {current}")
        self.current = current


async def _commit_new_revision(
    session: AsyncSession,
    note: Note,
    atoms: list[dict],
    cause: str,
    *,
    learner_edit_md: str | None = None,
) -> None:
    """Advance the note to a new substrate revision; drops all cached renders."""
    note.substrate = atoms
    note.revision_ordinal += 1
    session.add(
        NoteRevision(
            note_id=note.id,
            ordinal=note.revision_ordinal,
            substrate=atoms,
            cause=cause,
            learner_edit_md=learner_edit_md,
        )
    )
    await session.execute(delete(NoteRender).where(NoteRender.note_id == note.id))
    await session.flush()


async def refresh_note(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic
) -> NoteView:
    """The catch-up: distill anything past the per-stream cursors, then ensure a render exists."""
    note = await get_note(session, learner_id, topic.id)
    fmt = await effective_format(session, learner_id, note)
    messages_watermark, events_watermark = _cursors(note)

    if not await _has_new_activity(
        session, learner_id, topic, messages_watermark, events_watermark
    ):
        # Render-only heal (render missing for a current substrate), or nothing to do.
        if note is not None and note.revision_ordinal > 0:
            if await _current_render(session, note, fmt) is None:
                await _render_and_cache(session, llm, learner_id, note, fmt)
                await session.commit()
        return await _view(session, learner_id, topic, note)

    gathered = await _gather(session, learner_id, topic, messages_watermark, events_watermark)
    atoms = note.substrate if note is not None else []
    result, usage = await note_distill.distill(
        llm,
        topic=await _topic_context(session, topic),
        atoms=atoms,
        transcript=gathered.transcript,
        outcomes=gathered.outcomes,
        refs=gathered.refs,
    )
    await log_llm_call(
        learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )

    if result is None:
        # Parse failure or learner-atom violation: keep everything, stay stale, retry later.
        # Nothing to commit — the call this paid for is already recorded on its own
        # transaction, which is the point of accounting living outside this one.
        return await _view(session, learner_id, topic, note)

    if note is None:
        note = Note(
            learner_id=learner_id,
            topic_id=topic.id,
            substrate=[],
            messages_watermark=gathered.messages_watermark,
            events_watermark=gathered.events_watermark,
        )
        session.add(note)
        await session.flush()
    else:
        note.messages_watermark = gathered.messages_watermark
        note.events_watermark = gathered.events_watermark

    if result.no_change:
        await session.commit()
        # the note's cursors just changed -> the row's onupdate=func.now() updated_at is expired;
        # refresh before _view reads it (a bare attribute access can't await the reload).
        await session.refresh(note)
        return await _view(session, learner_id, topic, note)

    assert result.atoms is not None
    await _commit_new_revision(session, note, result.atoms, "distill")
    await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    await session.refresh(note)  # same reason: note was updated, updated_at is expired
    return await _view(session, learner_id, topic, note)


async def absorb_edit(
    session: AsyncSession,
    llm: LLMClient,
    learner_id: uuid.UUID,
    topic: Topic,
    content_md: str,
    *,
    expected_revision_ordinal: int | None = None,
) -> NoteView | None:
    """Fold a learner's edit into the substrate. None = no note yet, or absorb failed.

    ``expected_revision_ordinal`` is the revision the learner was editing. Supplying it turns a
    concurrent change — a refresh, or another tab — into an explicit
    :class:`RevisionConflict` instead of silently absorbing an edit against a note that no
    longer looks like what they saw. Checked again immediately before the write, so a change
    landing during the model call is caught too.
    """
    note = await get_note(session, learner_id, topic.id)
    if note is None or note.revision_ordinal == 0:
        return None
    if expected_revision_ordinal is not None and note.revision_ordinal != expected_revision_ordinal:
        raise RevisionConflict(note.revision_ordinal)
    fmt = await effective_format(session, learner_id, note)
    render_row = await _current_render(session, note, fmt)
    previous = (
        render_row.content_md if render_row else note_distill.mechanical_render(note.substrate)
    )
    atoms, usage = await note_distill.absorb(
        llm, atoms=note.substrate, previous_render=previous, edited_md=content_md
    )
    await log_llm_call(
        learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )
    if atoms is None:
        return None  # note untouched; the paid call is already recorded independently
    # Re-check after the model call: absorb takes seconds, and a refresh can land inside it.
    await session.refresh(note)
    if expected_revision_ordinal is not None and note.revision_ordinal != expected_revision_ordinal:
        raise RevisionConflict(note.revision_ordinal)
    await _commit_new_revision(session, note, atoms, "learner_edit", learner_edit_md=content_md)
    await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    await session.refresh(note)  # note was updated; onupdate=func.now() expired updated_at
    return await _view(session, learner_id, topic, note)


async def set_format(
    session: AsyncSession,
    llm: LLMClient,
    learner_id: uuid.UUID,
    topic: Topic,
    note_format: str | None,
) -> NoteView:
    """Set (or clear, None=auto) the explicit format; render the new format if missing."""
    note = await get_note(session, learner_id, topic.id)
    if note is None:
        note = Note(
            learner_id=learner_id,
            topic_id=topic.id,
            substrate=[],
            messages_watermark=EPOCH,
            events_watermark=EPOCH,
        )
        session.add(note)
        await session.flush()
    note.format = note_format
    fmt = await effective_format(session, learner_id, note)
    if note.revision_ordinal > 0 and await _current_render(session, note, fmt) is None:
        await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    await session.refresh(note)  # note.format was set; onupdate=func.now() expired updated_at
    return await _view(session, learner_id, topic, note)


async def list_revisions(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic
) -> list[NoteRevision]:
    note = await get_note(session, learner_id, topic.id)
    if note is None:
        return []
    rows = await session.scalars(
        select(NoteRevision).where(NoteRevision.note_id == note.id).order_by(NoteRevision.ordinal)
    )
    return list(rows.all())


async def _revision(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, ordinal: int
) -> NoteRevision | None:
    note = await get_note(session, learner_id, topic.id)
    if note is None:
        return None
    return await session.scalar(
        select(NoteRevision).where(NoteRevision.note_id == note.id, NoteRevision.ordinal == ordinal)
    )


async def revision_source(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, ordinal: int
) -> tuple[str, str | None] | None:
    """(the substrate rendered mechanically, the learner's own submitted markdown if any).

    The second is what they actually typed on a ``learner_edit`` revision — absorb reinterprets
    an edit, so this is the only place the original survives.
    """
    revision = await _revision(session, learner_id, topic, ordinal)
    if revision is None:
        return None
    return note_distill.mechanical_render(revision.substrate), revision.learner_edit_md


async def restore_revision(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic, ordinal: int
) -> NoteView | None:
    """Copy an old revision's substrate forward as a NEW revision — history is never rewritten."""
    revision = await _revision(session, learner_id, topic, ordinal)
    if revision is None:
        return None
    note = await get_note(session, learner_id, topic.id)
    assert note is not None  # _revision resolved through it
    await _commit_new_revision(session, note, revision.substrate, "restore")
    fmt = await effective_format(session, learner_id, note)
    await _render_and_cache(session, llm, learner_id, note, fmt)
    await session.commit()
    await session.refresh(note)  # note was updated; onupdate=func.now() expired updated_at
    return await _view(session, learner_id, topic, note)


async def notes_index(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> list[dict]:
    """Every topic in a subject with its note's freshness — a fixed number of queries (S62).

    This used to run six or so per topic: the note, two profile dimensions (both
    learner-global, so identical every time round the loop), two existence probes for new
    activity, and a render lookup. The probes are the interesting ones — the message probe's
    condition is subject-wide, so it asked the same question once per topic and got the same
    answer.

    Each per-topic question is now asked once for the whole subject and compared in memory:
    an existence test against a watermark is exactly a comparison against the newest row, and
    one grouped `max` answers it for every topic at once.
    """
    topics = (
        await session.scalars(
            select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.name)
        )
    ).all()
    if not topics:
        return []
    topic_ids = [topic.id for topic in topics]

    notes = {
        note.topic_id: note
        for note in (
            await session.scalars(
                select(Note).where(Note.learner_id == learner_id, Note.topic_id.in_(topic_ids))
            )
        ).all()
    }
    learned_format, conceptual_share = await _format_inputs(session, learner_id)
    latest_event = {
        topic_id: at
        for topic_id, at in (
            await session.execute(
                select(KC.topic_id, func.max(LearningEvent.created_at))
                .join(LearningEvent, LearningEvent.kc_id == KC.id)
                .where(
                    KC.topic_id.in_(topic_ids),
                    LearningEvent.learner_id == learner_id,
                    LearningEvent.event_type == "observation",
                )
                .group_by(KC.topic_id)
            )
        ).all()
    }
    # Subject-wide, so one answer serves every topic — which is what the per-topic probe was
    # computing over and over.
    latest_message = await session.scalar(
        select(func.max(Message.created_at))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.learner_id == learner_id, Conversation.subject_id == subject_id)
    )
    rendered = {
        (render.note_id, render.revision_ordinal, render.format)
        for render in (
            await session.scalars(
                select(NoteRender).where(NoteRender.note_id.in_([n.id for n in notes.values()]))
            )
        ).all()
    }

    entries: list[dict] = []
    for topic in topics:
        note = notes.get(topic.id)
        fmt = _format_from(note, learned_format, conceptual_share)
        messages_watermark, events_watermark = _cursors(note)
        has_note = note is not None and note.revision_ordinal > 0
        stale = (latest_event.get(topic.id) or EPOCH) > events_watermark or (
            latest_message or EPOCH
        ) > messages_watermark
        if not stale and has_note and note is not None:
            # Render-failure recovery: substrate current but no cached render for this format.
            stale = (note.id, note.revision_ordinal, fmt) not in rendered
        entries.append(
            {
                "topic_id": topic.id,
                "topic_name": topic.name,
                "has_note": has_note,
                "stale": stale,
                "updated_at": note.updated_at if note is not None else None,
            }
        )
    return entries
