"""Notes orchestration: staleness, catch-up distillation, edits, history (Phase 8).

Owns transactions and cost logging; all model-reply handling lives in
``app/learning/note_distill.py``. GET-path functions (``note_view``, ``notes_index``) are
pure reads — the work happens behind explicit refresh/edit calls, avoiding the
"GET with generation side-effects" compromise the reviews-due endpoint had to accept.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import note_distill
from app.learning.note_distill import FALLBACK_FORMAT, FORMATS, NOTES_ROLE
from app.llm import LLMClient
from app.models.assessment import Item
from app.models.chat import Conversation, Message
from app.models.knowledge import KC, Topic
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


async def effective_format(session: AsyncSession, learner_id: uuid.UUID, note: Note | None) -> str:
    """The cascade: explicit choice > learned note_format dimension > heuristic > outline."""
    if note is not None and note.format:
        return note.format
    learned = await _dimension_value(session, learner_id, "note_format")
    if isinstance(learned, str) and learned in FORMATS:
        return learned
    # Heuristic: a majority-conceptual error profile benefits from example-led notes.
    errors = await _dimension_value(session, learner_id, "error_type")
    conceptual = errors.get("conceptual", 0) if isinstance(errors, dict) else None
    if isinstance(conceptual, int | float) and conceptual >= 0.5:
        return "worked_examples"
    return FALLBACK_FORMAT


async def _reading_level(session: AsyncSession, learner_id: uuid.UUID) -> object:
    return await _dimension_value(session, learner_id, "reading_level")


async def _has_new_activity(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, watermark: datetime
) -> bool:
    kc_ids = select(KC.id).where(KC.topic_id == topic.id).scalar_subquery()
    event = await session.scalar(
        select(LearningEvent.id)
        .where(
            LearningEvent.learner_id == learner_id,
            LearningEvent.kc_id.in_(kc_ids),
            LearningEvent.created_at > watermark,
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
            Message.created_at > watermark,
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
    watermark = note.watermark if note is not None else EPOCH
    if await _has_new_activity(session, learner_id, topic, watermark):
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
    latest: datetime | None


async def _gather(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, watermark: datetime
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
                LearningEvent.created_at > watermark,
                LearningEvent.event_type == "observation",
            )
            .order_by(LearningEvent.created_at)
            .limit(settings.note_distill_max_outcome_events)
        )
    ).all()

    item_ids = {uuid.UUID(e.payload["item_id"]) for e in events if e.payload.get("item_id")}
    items: dict[uuid.UUID, Item] = {}
    if item_ids:
        rows = (await session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
        items = {item.id: item for item in rows}

    outcome_lines: list[str] = []
    for event in events:
        kc_name = kc_names.get(event.kc_id, "?")
        line = f"- KC '{kc_name}': score={event.payload.get('score')}, hints={event.payload.get('hints_used', 0)}"
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
                Message.created_at > watermark,
            )
            .order_by(Message.created_at)
            .limit(settings.note_distill_max_messages)
        )
    ).all()
    transcript_lines = [f"{m.role}: {m.content}" for m in messages]

    timestamps = [e.created_at for e in events] + [m.created_at for m in messages]
    return _Gathered(
        transcript="\n".join(transcript_lines),
        outcomes="\n".join(outcome_lines),
        latest=max(timestamps) if timestamps else None,
    )


async def _render_and_cache(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, note: Note, fmt: str
) -> NoteRender:
    content, usage = await note_distill.render(
        llm,
        atoms=note.substrate,
        note_format=fmt,
        reading_level=await _reading_level(session, learner_id),
    )
    await log_llm_call(
        session, learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )
    render_row = NoteRender(
        note_id=note.id, revision_ordinal=note.revision_ordinal, format=fmt, content_md=content
    )
    session.add(render_row)
    await session.flush()
    return render_row


async def _commit_new_revision(
    session: AsyncSession, note: Note, atoms: list[dict], cause: str
) -> None:
    """Advance the note to a new substrate revision; drops all cached renders."""
    note.substrate = atoms
    note.revision_ordinal += 1
    session.add(
        NoteRevision(note_id=note.id, ordinal=note.revision_ordinal, substrate=atoms, cause=cause)
    )
    await session.execute(delete(NoteRender).where(NoteRender.note_id == note.id))
    await session.flush()


async def refresh_note(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic
) -> NoteView:
    """The catch-up: distill anything past the watermark, then ensure a render exists."""
    note = await get_note(session, learner_id, topic.id)
    fmt = await effective_format(session, learner_id, note)
    watermark = note.watermark if note is not None else EPOCH

    if not await _has_new_activity(session, learner_id, topic, watermark):
        # Render-only heal (render missing for a current substrate), or nothing to do.
        if note is not None and note.revision_ordinal > 0:
            if await _current_render(session, note, fmt) is None:
                await _render_and_cache(session, llm, learner_id, note, fmt)
                await session.commit()
        return await _view(session, learner_id, topic, note)

    gathered = await _gather(session, learner_id, topic, watermark)
    atoms = note.substrate if note is not None else []
    result, usage = await note_distill.distill(
        llm,
        atoms=atoms,
        transcript=gathered.transcript,
        outcomes=gathered.outcomes,
        reading_level=await _reading_level(session, learner_id),
    )
    await log_llm_call(
        session, learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )

    if result is None:
        # Parse failure or learner-atom violation: keep everything, stay stale, retry later.
        await session.commit()  # persist the cost log
        return await _view(session, learner_id, topic, note)

    new_watermark = gathered.latest or _now()
    if note is None:
        note = Note(learner_id=learner_id, topic_id=topic.id, substrate=[], watermark=new_watermark)
        session.add(note)
        await session.flush()
    else:
        note.watermark = new_watermark

    if result.no_change:
        await session.commit()
        # note.watermark just changed -> the row's onupdate=func.now() updated_at is expired;
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
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, topic: Topic, content_md: str
) -> NoteView | None:
    """Fold a learner's edit into the substrate. None = no note yet, or absorb failed."""
    note = await get_note(session, learner_id, topic.id)
    if note is None or note.revision_ordinal == 0:
        return None
    fmt = await effective_format(session, learner_id, note)
    render_row = await _current_render(session, note, fmt)
    previous = (
        render_row.content_md if render_row else note_distill.mechanical_render(note.substrate)
    )
    atoms, usage = await note_distill.absorb(
        llm, atoms=note.substrate, previous_render=previous, edited_md=content_md
    )
    await log_llm_call(
        session, learner_id=learner_id, role=str(NOTES_ROLE), spec=llm.spec(NOTES_ROLE), usage=usage
    )
    if atoms is None:
        await session.commit()  # persist the cost log; note untouched
        return None
    await _commit_new_revision(session, note, atoms, "learner_edit")
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
        note = Note(learner_id=learner_id, topic_id=topic.id, substrate=[], watermark=EPOCH)
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
) -> str | None:
    revision = await _revision(session, learner_id, topic, ordinal)
    return note_distill.mechanical_render(revision.substrate) if revision is not None else None


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
    topics = (
        await session.scalars(
            select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.name)
        )
    ).all()
    entries: list[dict] = []
    for topic in topics:
        note = await get_note(session, learner_id, topic.id)
        fmt = await effective_format(session, learner_id, note)
        entries.append(
            {
                "topic_id": topic.id,
                "topic_name": topic.name,
                "has_note": note is not None and note.revision_ordinal > 0,
                "stale": await _is_stale(session, learner_id, topic, note, fmt),
                "updated_at": note.updated_at if note is not None else None,
            }
        )
    return entries
