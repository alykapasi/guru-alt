"""Request/response schemas for chat."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = None
    # "session" conversations (created by the Lessons page's "Start practice") always run the
    # guided-practice workflow and are never shown behind the plain chat gate/composer — see
    # Conversation.kind. Set once at creation, never changed.
    kind: Literal["chat", "session"] = "chat"
    # Set once at creation, never changed. Scopes plan grounding + the session runner's
    # practice item to this exact subject instead of a cross-subject heuristic. Also the hard
    # retrieval/citation boundary (Phase 7): None = "general", no library grounding at all.
    subject_id: uuid.UUID | None = None
    # Narrows retrieval to specific sources within `subject_id`'s materials — empty means "all
    # sources under this subject." Must be empty when subject_id is None (nothing to narrow).
    source_ids: list[uuid.UUID] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    learner_id: uuid.UUID
    title: str | None
    kind: str
    goal: str | None
    subject_id: uuid.UUID | None
    source_ids: list[uuid.UUID]
    # What the conversation is waiting for, and the practice item in play if it is waiting on
    # one — recorded by the turn that produced the last reply, so a reload restores what the
    # live stream showed instead of guessing from the transcript's shape (S52).
    phase: str
    active_item_id: uuid.UUID | None
    created_at: datetime


class CheckComponentRead(BaseModel):
    """One knowledge component of a graded conversational answer, as the learner sees it.

    Every optional field is optional because the evidence genuinely may not exist, not because
    it was inconvenient to fill in. ``score`` is null when the grader could not tell the
    components apart — an MCQ has one outcome (S10) — and ``failure_kind`` is null when nothing
    diagnosed it, which is every deterministic path (S09). Rendering must keep that difference:
    "we could not tell" and "it was fine" are not the same thing to say to a learner.
    """

    kc_id: uuid.UUID
    kc_name: str
    score: float | None = None
    # Ability on the logit scale, before and after this answer. Both, because a posterior on
    # its own gives the learner no baseline to read it against.
    prior_ability: float
    ability: float
    uncertainty: float
    failure_kind: str | None = None
    # The grader's own sentence about what went wrong. Shown as written rather than
    # paraphrased; confidence is deliberately not exposed, because a language model's
    # self-reported confidence is not calibrated and a number implies it is (S09).
    failure_detail: str | None = None
    # How many *earlier* attempts on this component failed the same way. Null when nothing was
    # diagnosed or nothing matches; 0 when this is the first time. It is here rather than only
    # in the tutor's instruction because a learner is owed the same distinction the teaching
    # makes: a slip and a settled wrong idea look identical on one attempt, and knowing which
    # one this is changes what they should do about it.
    recurrence: int | None = None


class CheckResultRead(BaseModel):
    """What happened to an answer the learner gave in conversation (S15).

    Until this existed, a conversational answer was graded, updated mastery, rescheduled the
    card and revised the plan — and the learner was told none of it. The tutor's reply was the
    only evidence anything had happened, and a reply is not a record.
    """

    item_id: uuid.UUID
    score: float
    correct: bool
    components: list[CheckComponentRead]


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    model: str | None
    # Each entry: {"marker": int, "chunk_id": str, "source_id": str} — see Message.citations.
    citations: list[dict]
    # The grade this reply reported, for the turns that marked an answer (S15). Null on every
    # other message, and on every message written before this was stored — an older transcript
    # legitimately has no report, and inventing one from the event log would be reconstructing
    # a statement rather than recalling it.
    check_result: CheckResultRead | None = None
    created_at: datetime


class TurnRead(BaseModel):
    """One recorded attempt at answering one learner message (S51).

    ``status`` is what makes an interruption visible: a turn that ended without producing a
    reply reads as ``failed`` or ``cancelled`` here, where the transcript alone would just
    stop. Re-sending the message with the same ``client_turn_id`` retries *this* turn.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_turn_id: uuid.UUID | None
    flow: str  # refinement | tutor | agentic | workflow
    status: str  # pending | completed | failed | cancelled
    content: str
    assistant_message_id: uuid.UUID | None
    error: str | None
    created_at: datetime


class ChatTurnRequest(BaseModel):
    # A maximum as well as a minimum: an unbounded message is a paid call whose size the
    # learner chooses. Rejected here, before the turn reaches a provider. The bound is a
    # config value (`chat_max_input_chars`) mirrored as a literal because Pydantic
    # constraints are class-level; the two are kept in step by test_chat_budget.py.
    content: str = Field(min_length=1, max_length=20_000)
    # Explicit learner acceptance of the refinement gate's latest proposal. Ignored once a
    # conversation's goal is already committed (or no gate is in progress).
    satisfied: bool = False
    # "agentic" is a one-off tool-using action for this turn only — it bypasses the
    # refinement gate regardless of the conversation's goal/gate state. "workflow" starts (or,
    # if already sent, is overridden by) the guided-practice workflow, which likewise
    # presupposes a committed goal + plan and so also bypasses the gate. Per-turn rather than
    # persisted on the conversation: no migration, and a learner can mix one tool-using or
    # workflow turn into an otherwise plain conversation.
    mode: Literal["chat", "agentic", "workflow"] = "chat"
    # The client's idempotency key for this turn, so a retry after a dropped stream is *this*
    # turn again rather than a second turn asking the same thing (S51). Optional: a turn sent
    # without one is unconstrained, exactly as before this existed.
    client_turn_id: uuid.UUID | None = None
