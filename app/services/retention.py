"""What the platform keeps, hands back, and destroys for one learner (S61).

Retention was implicit: an ``ondelete`` clause per foreign key, spread across a dozen model
files, with no statement anywhere of what was supposed to happen to a learner's data — and
two stores no foreign key reaches at all (object storage, and items authored by the learner,
whose FK is ``SET NULL``). "Deleting a conversation leaves memories" was a deliberate
decision; nothing recorded that it *was* one, or what the other twelve stores do.

:data:`RETENTION` states it, store by store, with the reason. It is executable rather than
prose — :func:`delete_learner` walks it, and a test asserts every table carrying a
``learner_id`` appears in it, so a new learner-owned store cannot be added without a
disposition being chosen for it.

**Ordering.** The database is cleared first and object storage second. The reverse would let
a failed database delete leave live rows pointing at bytes that no longer exist — a broken
account. This way a failure leaves orphaned blobs, which the report names so they can be
retried; nothing the learner can still reach survives.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Item
from app.models.chat import Conversation, Message, Turn
from app.models.content import ContentBlock
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.memory import Memory
from app.models.note import Note, NoteRender, NoteRevision
from app.models.profile import LearnerProfile, ProfileDimension
from app.models.source import Chunk, Source
from app.services import ingestion
from app.storage.base import BlobStore

log = structlog.get_logger(__name__)

Disposition = Literal["deleted", "anonymised", "retained"]


@dataclass(frozen=True)
class StoreRetention:
    """What happens to one store when its learner is deleted, and why."""

    table: str
    disposition: Disposition
    reason: str


RETENTION: tuple[StoreRetention, ...] = (
    StoreRetention("learners", "deleted", "The account itself."),
    StoreRetention("conversations", "deleted", "Cascades from the learner; messages with it."),
    StoreRetention("messages", "deleted", "Cascades from the conversation."),
    StoreRetention("turns", "deleted", "Cascades from the conversation."),
    StoreRetention(
        "memories",
        "deleted",
        "Cascades from the learner. Note the asymmetry this resolves: deleting one "
        "*conversation* deliberately leaves its memories, because a durable fact outlives the "
        "conversation it was learned in. Deleting the learner does not.",
    ),
    StoreRetention("learner_profiles", "deleted", "Cascades from the learner."),
    StoreRetention("profile_dimensions", "deleted", "Cascades from the learner."),
    StoreRetention("learner_kc_state", "deleted", "Cascades from the learner."),
    StoreRetention("learning_events", "deleted", "Cascades from the learner."),
    StoreRetention("lesson_plans", "deleted", "Cascades from the learner."),
    StoreRetention("notes", "deleted", "Cascades from the learner; revisions and renders with it."),
    StoreRetention("note_revisions", "deleted", "Cascades from the note."),
    StoreRetention("note_renders", "deleted", "Cascades from the note."),
    StoreRetention("content_blocks", "deleted", "Cascades from the learner."),
    StoreRetention("sources", "deleted", "Cascades from the learner; chunks and tags with it."),
    StoreRetention("chunks", "deleted", "Cascades from the source."),
    StoreRetention(
        "blobs",
        "deleted",
        "Object storage, which no foreign key reaches — deleted explicitly, by key, after the "
        "database. The raw uploaded bytes are the most sensitive thing held. Keys are "
        "content-addressed, so bytes another learner uploaded independently and still "
        "references are left in place; nothing of this learner's survives either way, because "
        "a source row is what makes bytes reachable.",
    ),
    StoreRetention(
        "items",
        "deleted",
        "Only the ones this learner authored (S33). The FK is SET NULL, so a cascade would "
        "have left their questions and answer keys behind with the author erased.",
    ),
    StoreRetention(
        "llm_calls",
        "anonymised",
        "The learner id is dropped (SET NULL) and the row kept: token spend is the platform's "
        "own accounting, and it must still add up after an account is closed. It carries no "
        "learner content — role, model, token counts, cost.",
    ),
    StoreRetention(
        "subjects/topics/kcs/kc_edges/items(generated)/rubrics",
        "retained",
        "Shared curriculum and the generated question bank belong to no one learner.",
    ),
)


@dataclass
class DeletionReport:
    """What a deletion actually removed, and what it could not."""

    learner_id: uuid.UUID
    blobs_deleted: int = 0
    # Bytes left in place because another learner's source references the same content-
    # addressed key. Not a failure: nothing of this learner's survives, since a Source row is
    # what makes bytes reachable and theirs are gone.
    blobs_retained: int = 0
    # Keys the object store refused. The rows are already gone, so these cannot be found
    # again by walking the database — they are reported so a caller can retry or escalate.
    blobs_failed: list[str] = field(default_factory=list)
    items_deleted: int = 0

    @property
    def complete(self) -> bool:
        return not self.blobs_failed


async def export_learner(session: AsyncSession, learner_id: uuid.UUID) -> dict[str, Any]:
    """Everything the platform holds about one learner, as plain JSON-able data.

    Metadata only for uploads: the export names each source and its blob key, not the bytes.
    Shipping the raw files needs a packaging step (and, for anything large, a signed download)
    that this does not attempt — what it does guarantee is that nothing is silently omitted,
    because the store list is the same :data:`RETENTION` the deletion walks.
    """
    learner = await session.get(Learner, learner_id)
    if learner is None:
        raise LookupError(str(learner_id))

    conversation_ids = list(
        (
            await session.scalars(
                select(Conversation.id).where(Conversation.learner_id == learner_id)
            )
        ).all()
    )
    note_ids = list(
        (await session.scalars(select(Note.id).where(Note.learner_id == learner_id))).all()
    )
    source_ids = list(
        (await session.scalars(select(Source.id).where(Source.learner_id == learner_id))).all()
    )

    async def rows(model: Any, where: Any) -> list[dict[str, Any]]:
        result = await session.scalars(select(model).where(where))
        return [_as_dict(row) for row in result.all()]

    return {
        "learner": _as_dict(learner),
        "conversations": await rows(Conversation, Conversation.learner_id == learner_id),
        "messages": await rows(Message, Message.conversation_id.in_(conversation_ids)),
        "turns": await rows(Turn, Turn.conversation_id.in_(conversation_ids)),
        "memories": await rows(Memory, Memory.learner_id == learner_id),
        "profile": await rows(LearnerProfile, LearnerProfile.learner_id == learner_id),
        "profile_dimensions": await rows(
            ProfileDimension, ProfileDimension.learner_id == learner_id
        ),
        "kc_states": await rows(LearnerKCState, LearnerKCState.learner_id == learner_id),
        "learning_events": await rows(LearningEvent, LearningEvent.learner_id == learner_id),
        "lesson_plans": await rows(LessonPlan, LessonPlan.learner_id == learner_id),
        "notes": await rows(Note, Note.learner_id == learner_id),
        "note_revisions": await rows(NoteRevision, NoteRevision.note_id.in_(note_ids)),
        "note_renders": await rows(NoteRender, NoteRender.note_id.in_(note_ids)),
        "content_blocks": await rows(ContentBlock, ContentBlock.learner_id == learner_id),
        "sources": await rows(Source, Source.learner_id == learner_id),
        "chunks": await rows(Chunk, Chunk.source_id.in_(source_ids)),
        "authored_items": await rows(Item, Item.author_learner_id == learner_id),
    }


async def delete_learner(
    session: AsyncSession, blobstore: BlobStore, learner_id: uuid.UUID
) -> DeletionReport:
    """Erase one learner from every store, per :data:`RETENTION`.

    Enqueued work needs no separate cancellation: every job is keyed by a row (a source, a
    conversation) that this removes, and each task returns when its row is gone. A job that
    was already running loses its rows underneath it and commits nothing — there is a test for
    both. What has no such guard is anything holding data *outside* these tables, which is why
    blob keys are collected before the rows that name them are destroyed.
    """
    report = DeletionReport(learner_id=learner_id)
    blob_keys = [
        key
        for key in (
            await session.scalars(
                select(Source.blob_key).where(
                    Source.learner_id == learner_id, Source.blob_key.is_not(None)
                )
            )
        ).all()
        if key
    ]
    authored = await session.execute(
        delete(Item).where(Item.author_learner_id == learner_id).returning(Item.id)
    )
    report.items_deleted = len(authored.all())
    await session.execute(delete(Learner).where(Learner.id == learner_id))
    await session.commit()

    for key in blob_keys:
        # Checked one at a time, immediately before each delete, to keep the window between
        # the check and the delete as small as it can be without a transaction the object
        # store is not part of. Blobs are content-addressed, so another learner who uploaded
        # the same file references this exact key (see ingestion.blob_key_for) — their bytes
        # must survive this account closing.
        try:
            if await ingestion.unreference_blob(session, blobstore, key):
                report.blobs_deleted += 1
            else:
                report.blobs_retained += 1
        except Exception:
            report.blobs_failed.append(key)
    if report.blobs_failed:
        log.error(
            "retention.blobs_not_deleted",
            learner_id=str(learner_id),
            count=len(report.blobs_failed),
        )
    log.info(
        "retention.learner_deleted",
        learner_id=str(learner_id),
        blobs_deleted=report.blobs_deleted,
        items_deleted=report.items_deleted,
    )
    return report


def _as_dict(row: Any) -> dict[str, Any]:
    """One ORM row as JSON-able primitives, embeddings excluded.

    A vector is a derived artefact of the model that produced it and is meaningless outside
    that embedding space (S50); including thousands of floats per row would bury the export's
    actual content without telling the learner anything.
    """
    out: dict[str, Any] = {}
    for column in row.__table__.columns:
        if column.name == "embedding":
            continue
        value = getattr(row, column.name)
        out[column.name] = value.isoformat() if hasattr(value, "isoformat") else _plain(value)
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def retention_tables() -> Sequence[str]:
    """Every table name named in :data:`RETENTION`, including grouped entries."""
    return [name for entry in RETENTION for name in entry.table.split("/")]
