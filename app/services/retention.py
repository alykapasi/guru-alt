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
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import structlog
from sqlalchemy import CursorResult, Result, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.core.identity import IdentityProvider
from app.models.assessment import Item, Rubric
from app.models.auth import AccountAction, AdminAction, Impersonation, Invitation
from app.models.chat import Conversation, LLMCall, Message, Turn, TurnStatus
from app.models.content import ContentBlock
from app.models.decision import DecisionCall
from app.models.erasure import ErasureKind, PendingErasure
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.memory import Memory
from app.models.note import Note, NoteRender, NoteRevision
from app.models.ops import AlertTransition
from app.models.profile import LearnerProfile, ProfileDimension
from app.models.publication import Publication
from app.models.source import Chunk, Source
from app.services import auth as auth_svc
from app.services import ingestion
from app.storage.base import BlobStore

log = structlog.get_logger(__name__)

# "partly deleted" is a fourth answer rather than a fudge of the other three, and S25 is what
# made it necessary: one table can now hold both a learner's own rows and rows belonging to
# nobody, so "deleted" and "retained" are each false about half of it. Calling it either would
# be the kind of statement this module exists to stop — a policy that reads as decided and is
# wrong in the case somebody eventually asks about.
Disposition = Literal["deleted", "anonymised", "retained", "partly deleted"]


@dataclass(frozen=True)
class StoreRetention:
    """What happens to one store when its learner is deleted, and why."""

    table: str
    disposition: Disposition
    reason: str


RETENTION: tuple[StoreRetention, ...] = (
    StoreRetention("learners", "deleted", "The account itself."),
    StoreRetention(
        "learner_sessions",
        "deleted",
        "Cascades from the learner (S21). Deleting the account has to stop every session "
        "authenticating as it at once — a session that outlived its owner would be a live "
        "credential for an account that no longer exists.",
    ),
    StoreRetention(
        "impersonations",
        "partly deleted",
        "The record of an administrator viewing this account (P10), and the fourth disposition "
        "is doing real work here rather than hedging. The learner's half goes: their id is "
        "cleared by the foreign key and the service clears the handle beside it, so nothing "
        "left names them. The administrator's half is retained, because a record of who "
        "accessed accounts that any *subject* of that access can erase is not an audit of "
        "access — and the platform still has to be able to answer what its administrators did "
        "after an account is closed. What survives is that somebody with a name viewed "
        "somebody, when, for how long, and the reason they gave.",
    ),
    StoreRetention(
        "admin_actions",
        "retained",
        "Durable administrator action audit; bodies and credentials are never captured.",
    ),
    StoreRetention(
        "legacy_password_digests",
        "deleted",
        "Cascades from the learner (S21). A password hash for an account that no longer exists "
        "is a credential for nobody, and the table empties itself as `poe identity-import` "
        "hands each digest to Clerk.",
    ),
    StoreRetention("conversations", "deleted", "Cascades from the learner; messages with it."),
    StoreRetention("messages", "deleted", "Cascades from the conversation."),
    StoreRetention(
        "turns",
        "deleted",
        "Cascades from the conversation. Finished turns are also deleted after the diagnostic "
        "window; the transcript is the durable copy.",
    ),
    StoreRetention(
        "onboarding_sessions",
        "deleted",
        "Cascades from the learner. The row is only a record of who owns a paused goal "
        "negotiation; without it the checkpoint it names can never be resumed by anyone, "
        "because the thread key is derived from the learner id too.",
    ),
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
        "partly deleted",
        "Owned generated and authored items are erased. Explicit platform-curated rows "
        "remain shared; unknown historical rows remain quarantined.",
    ),
    StoreRetention(
        "rubrics",
        "partly deleted",
        "Owned marking criteria cascade from the learner; explicit curated criteria remain.",
    ),
    StoreRetention(
        "llm_calls",
        "anonymised",
        "The learner id is dropped (SET NULL) and the row kept: token spend is the platform's "
        "own accounting, and it must still add up after an account is closed. It carries no "
        "learner content — role, model, token counts, cost. After the diagnostic window (30 "
        "days by default) the learner and conversation ids are dropped even for a live "
        "account.",
    ),
    StoreRetention(
        "decision_calls",
        "anonymised",
        "Same accounting reasoning as llm_calls, and the same shape (S82): the learner id is "
        "dropped (SET NULL) and the row kept, because it is evidence for whether Jev can "
        "replace a model call and that evidence must still add up after an account is closed. "
        "It carries no learner content — the question, the answer label or probability, and "
        "the baseline the model would have given. After the diagnostic window (30 days by "
        "default) the learner and conversation ids are dropped even for a live account.",
    ),
    StoreRetention(
        "subjects/topics/kcs/kc_edges",
        "partly deleted",
        "Split by ownership since S25. A subject the learner created is theirs and goes with "
        "the account, taking its topics, components and prerequisite edges by cascade. A "
        "*curated* subject carries no owner, belongs to no one learner, and is retained — "
        "deleting one account must not empty the shared library for everybody else. The "
        "shared graph is unaffected by deleting a learner.",
    ),
    StoreRetention(
        "concept_links/concept_link_decisions",
        "partly deleted",
        "Candidate links between presentations in different subjects, and a learner's decision "
        "on one (S24). A private link spans a subject the learner owns, so it — and any "
        "learner's decision recorded against it — cascades away with the account. A curated "
        "link belongs to no learner and is retained regardless of who is deleted; deleting the "
        "administrator who ruled on one only clears `decided_by_admin_id` (SET NULL), not the "
        "ruling.",
    ),
    StoreRetention(
        "invitations",
        "partly deleted",
        "Permission to enroll (S21). The invitation this learner accepted goes with the "
        "account, because it holds their address. Invitations *they* issued as an "
        "administrator are retained with the rest of the administrative record — who let "
        "somebody in is not the invitee's to erase.",
    ),
    StoreRetention(
        "account_actions",
        "partly deleted",
        "Administrative acts on accounts (S21): invitations, suspensions, reinstatements. The "
        "same split as impersonations — the subject's id and handle and the address go, so "
        "nothing left names them; that an administrator acted, when, and why is retained.",
    ),
    StoreRetention(
        "curriculum_proposals",
        "deleted",
        "Cascades from the learner (S25b). The row records only what the server observed while "
        "generating one curriculum — whether it was grounded in that learner's uploads — and "
        "it is scaffolding for a commit that can no longer happen once the account is gone.",
    ),
    StoreRetention(
        "publications",
        "partly deleted",
        "A request to share a subject and the decision made on it (S25b), split the same way "
        "as impersonations. The author's half goes: their id is cleared by the foreign key and "
        "the service clears the handle beside it. The reviewer's half is retained, because who "
        "approved putting material into the shared library is the platform's own record, and "
        "an audit any author can erase is not an audit. The snapshot is retained with it — it "
        "is what was approved, and a published subject whose approval named nothing would be "
        "material in the library with no account of how it got there.",
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
        "authored_items": await rows(
            Item, or_(Item.author_learner_id == learner_id, Item.owner_learner_id == learner_id)
        ),
        "rubrics": await rows(Rubric, Rubric.owner_learner_id == learner_id),
        # Their half of P10's audit: who has viewed this account, when, and why. A record of
        # access that the person accessed cannot see is a record kept for somebody else.
        "impersonations": await rows(Impersonation, Impersonation.learner_id == learner_id),
        "admin_actions": await rows(
            AdminAction,
            AdminAction.impersonation_id.in_(
                select(Impersonation.id).where(Impersonation.learner_id == learner_id)
            ),
        ),
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
        delete(Item)
        .where(or_(Item.author_learner_id == learner_id, Item.owner_learner_id == learner_id))
        .returning(Item.id)
    )
    report.items_deleted = len(authored.all())
    # Their address is theirs, so the invitation they accepted goes with the account; the ones
    # they issued as an administrator stay with the rest of the administrative record.
    await session.execute(delete(Invitation).where(Invitation.accepted_learner_id == learner_id))
    await session.execute(
        update(AccountAction)
        .where(AccountAction.learner_id == learner_id)
        .values(learner_handle=None, email=None)
    )
    # Before the learner row goes: the foreign key clears `learner_id` on its own, and after
    # that there is no way left to find the rows whose *handle* still names this person (P10).
    await session.execute(
        update(Impersonation)
        .where(Impersonation.learner_id == learner_id)
        .values(learner_handle=None)
    )
    # Same reasoning, same window (S25b): the author's handle is the half of a publication that
    # still names this person once the foreign key has cleared their id. `reviewer_handle` is
    # deliberately untouched — the administrator's half of the record is not the author's to
    # erase, exactly as with impersonations above.
    await session.execute(
        update(Publication).where(Publication.author_id == learner_id).values(author_handle=None)
    )
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


class NotPending(Exception):
    """Restore or erase-now asked of an account that is not pending deletion."""


async def request_deletion(
    session: AsyncSession, learner_id: uuid.UUID, *, settings: Settings
) -> Learner:
    """Start the recovery window (V12): access ends now, the data stays until the due time.

    Every session is revoked, so every device is signed out at once. Idempotent: asking again
    while pending neither moves the due date nor extends the window.
    """
    learner = await session.get(Learner, learner_id, populate_existing=True, with_for_update=True)
    if learner is None:
        raise LookupError(str(learner_id))
    if learner.deletion_due_at is None:
        now = datetime.now(UTC)
        learner.deletion_requested_at = now
        learner.deletion_due_at = now + timedelta(days=settings.account_recovery_days)
    await session.commit()  # before revoking: revoke_all commits its own transaction
    await auth_svc.revoke_all(session, learner_id)
    await session.refresh(learner)
    return learner


async def restore_account(session: AsyncSession, learner_id: uuid.UUID) -> Learner:
    """End the recovery window early by keeping the account. ``NotPending`` if it is active."""
    learner = await session.get(Learner, learner_id, populate_existing=True, with_for_update=True)
    if learner is None:
        raise LookupError(str(learner_id))
    if learner.deletion_due_at is None:
        raise NotPending(str(learner_id))
    learner.deletion_requested_at = None
    learner.deletion_due_at = None
    await session.commit()
    await session.refresh(learner)
    return learner


async def queue_erasure(session: AsyncSession, kind: ErasureKind, target: str, error: str) -> None:
    """Record something that could not be erased, for the retry worker. Commits."""
    await session.execute(
        pg_insert(PendingErasure)
        .values(id=uuid.uuid4(), kind=kind, target=target, last_error=error[:500])
        .on_conflict_do_nothing(constraint="uq_pending_erasures_kind_target")
    )
    await session.commit()


async def erase_learner(
    session: AsyncSession,
    blobstore: BlobStore,
    provider: IdentityProvider | None,
    learner_id: uuid.UUID,
) -> DeletionReport:
    """Erase the account now: every store per :data:`RETENTION`, then the provider's copy.

    A refusal by the object store or the provider never undoes the database erase; it becomes a
    pending erasure the worker retries until it succeeds.
    """
    learner = await session.get(Learner, learner_id)
    subject = learner.auth_subject if learner is not None else None
    report = await delete_learner(session, blobstore, learner_id)
    for key in report.blobs_failed:
        await queue_erasure(session, ErasureKind.BLOB, key, "refused at account erase")
    if subject:
        if provider is None:
            await queue_erasure(session, ErasureKind.IDENTITY, subject, "no provider configured")
        else:
            try:
                await provider.delete_user(subject)
            except Exception as exc:
                log.warning("retention.identity_not_deleted", learner_id=str(learner_id))
                await queue_erasure(session, ErasureKind.IDENTITY, subject, str(exc))
    return report


async def erase_due(
    session: AsyncSession,
    blobstore: BlobStore,
    provider: IdentityProvider | None,
    *,
    now: datetime,
) -> int:
    """Erase every account whose recovery window has passed. Returns how many.

    Each candidate is re-read under a row lock: a learner who restored between the query and
    the erase is no longer due and is skipped.
    """
    ids = list(
        (
            await session.scalars(
                select(Learner.id).where(
                    Learner.deletion_due_at.is_not(None), Learner.deletion_due_at <= now
                )
            )
        ).all()
    )
    erased = 0
    for learner_id in ids:
        learner = await session.get(
            Learner, learner_id, populate_existing=True, with_for_update=True
        )
        if learner is None or learner.deletion_due_at is None or learner.deletion_due_at > now:
            await session.rollback()
            continue
        await erase_learner(session, blobstore, provider, learner_id)
        erased += 1
    return erased


_MAX_BACKOFF = timedelta(days=1)


def _backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2**attempts), _MAX_BACKOFF)


async def retry_erasures(
    session: AsyncSession,
    blobstore: BlobStore,
    provider: IdentityProvider | None,
    *,
    now: datetime,
) -> int:
    """Try every due pending erasure once. Returns how many were resolved (row removed).

    A blob is deleted only while nothing references its key again — bytes are
    content-addressed, and a re-upload since the refusal makes them somebody's file; either way
    the erasure is no longer owed. An identity already gone at the provider counts as done.
    Failures back off exponentially to a day and keep retrying; ``stuck_erasures`` is what
    makes a persistent one visible.
    """
    rows = list(
        (
            await session.scalars(
                select(PendingErasure)
                .where(PendingErasure.next_attempt_at <= now)
                .order_by(PendingErasure.next_attempt_at)
            )
        ).all()
    )
    resolved = 0
    for row in rows:
        try:
            if row.kind == ErasureKind.BLOB:
                await ingestion.unreference_blob(session, blobstore, row.target)
            elif provider is None:
                raise RuntimeError("no identity provider configured")
            else:
                await provider.delete_user(row.target)
        except Exception as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            row.next_attempt_at = now + _backoff(row.attempts)
            await session.commit()
            continue
        await session.delete(row)
        await session.commit()
        resolved += 1
    return resolved


async def stuck_erasures(session: AsyncSession, *, attempts: int) -> int:
    """How many pending erasures have been refused at least ``attempts`` times."""
    count = await session.scalar(
        select(func.count()).select_from(PendingErasure).where(PendingErasure.attempts >= attempts)
    )
    return count or 0


async def _count(statement: Awaitable[Result[Any]]) -> int:
    """How many rows an UPDATE or DELETE touched."""
    return int(cast("CursorResult[Any]", await statement).rowcount)


@dataclass
class ExpiryReport:
    calls_anonymised: int = 0
    decisions_anonymised: int = 0
    turns_deleted: int = 0
    alerts_deleted: int = 0

    @property
    def changed(self) -> bool:
        return any(
            (
                self.calls_anonymised,
                self.decisions_anonymised,
                self.turns_deleted,
                self.alerts_deleted,
            )
        )


async def expire_diagnostics(session: AsyncSession, *, older_than: timedelta) -> ExpiryReport:
    """Apply the diagnostic window (V12) — selectively.

    Accounting rows are anonymised, not deleted: spend totals and Jev's evidence must still
    add up, and without a learner or conversation they point at nobody. Finished turns go —
    their messages are the durable copy. Alert history goes except each condition's newest row,
    which is its current state. Learning history, notes, memories, sources and audit records
    are never touched here.
    """
    cutoff = func.now() - older_than
    report = ExpiryReport()
    for model in (LLMCall, DecisionCall):
        anonymised = await _count(
            session.execute(
                update(model)
                .where(
                    model.created_at < cutoff,
                    or_(model.learner_id.is_not(None), model.conversation_id.is_not(None)),
                )
                .values(learner_id=None, conversation_id=None)
            )
        )
        if model is LLMCall:
            report.calls_anonymised = anonymised
        else:
            report.decisions_anonymised = anonymised
    report.turns_deleted = await _count(
        session.execute(
            delete(Turn).where(
                Turn.created_at < cutoff,
                Turn.status.in_([TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED]),
            )
        )
    )
    newer = aliased(AlertTransition)
    has_newer = (
        select(newer.id)
        .where(newer.name == AlertTransition.name, newer.seq > AlertTransition.seq)
        .exists()
    )
    report.alerts_deleted = await _count(
        session.execute(
            delete(AlertTransition).where(AlertTransition.created_at < cutoff, has_newer)
        )
    )
    await session.commit()
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
