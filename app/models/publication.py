"""Publication: the record of a learner asking to share a subject, and what was decided (S25b).

A publication is not a link between two subjects — it is the *decision*, and it has to outlive
both of them. So every foreign key here is SET NULL and the handles are kept as text beside the
ids, the same shape `impersonations` uses for the same reason: an administrator asking "what did
we approve, and who approved it?" a year later should get an answer even after the author closed
their account and the subject was deleted.

The snapshot is the other half of that. It is frozen when the request is made and is what
approval materializes, so the thing the reviewer judged is the thing that shipped — not the
subject as it happens to look whenever somebody gets round to clicking approve.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class PublicationStatus(StrEnum):
    """Where a request has got to. Only ``PENDING`` is a state anybody is waiting on."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CurriculumProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What the server observed when it generated a curriculum (S25b D4).

    This row exists because the fact it holds cannot be trusted to the client. Whether a
    curriculum was grounded in the learner's own uploads decides whether the subject can ever be
    published, and the original design carried that as a bit in the proposal the browser got
    back — a bit you hand the client is a bit the client can drop, which makes the control an
    override with extra steps.

    So `/onboarding/curriculum` writes what it actually did here and returns only the id, and
    `/subjects/commit` reads grounding from this row. CASCADE rather than SET NULL: this is
    scaffolding for one learner's commit, not a record anybody audits afterwards.
    """

    __tablename__ = "curriculum_proposals"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    # True only when excerpts were actually retrieved and sent to the model. Asking for sources
    # that returned nothing is not grounding: no source text reached the curriculum, and
    # flagging it would retire an honest subject for no privacy gain.
    grounded_in_sources: Mapped[bool] = mapped_column(Boolean)


class Publication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One request to publish a subject, and the review it received."""

    __tablename__ = "publications"
    __table_args__ = (
        # One open request per subject, enforced in the database rather than by a service
        # check: two concurrent requests would both read "none pending" and both insert.
        Index(
            "uq_publications_one_pending",
            "source_subject_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    source_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), index=True, default=None
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    # Kept as text beside the id so the record survives the account, and nullable so that
    # closing the account can clear it — the author's half of this record is theirs to erase.
    # `reviewer_handle` is nullable for a different reason: nobody has reviewed it yet.
    author_handle: Mapped[str | None] = mapped_column(String, default=None)
    status: Mapped[str] = mapped_column(String, index=True, default=PublicationStatus.PENDING)
    author_note: Mapped[str | None] = mapped_column(Text, default=None)
    # Frozen at request time. Approval reads *this*, never the live subject — see the module
    # docstring, and D3 in the spec.
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)

    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), default=None
    )
    reviewer_handle: Mapped[str | None] = mapped_column(String, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review_note: Mapped[str | None] = mapped_column(Text, default=None)

    # Item ids the reviewer struck out before approving. Kept on the decision rather than
    # applied to the snapshot, so the snapshot stays exactly what the author submitted.
    excluded_item_ids: Mapped[list] = mapped_column(JSONB, default=list)
    published_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), default=None
    )
