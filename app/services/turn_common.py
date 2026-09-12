"""Shared helpers for streamed conversation turns (plain tutoring + the refinement gate).

Split out from ``chat.py`` so ``refinement.py`` can reuse them without the two service
modules importing each other.
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.untrusted import as_untrusted
from app.learning.diagnosis import FailureKind
from app.learning.grading import GradeResult
from app.learning.mastery import Estimate
from app.llm.types import ChatMessage, ChatRole, Usage
from app.models.chat import Message
from app.models.knowledge import KC
from app.models.learning import LearnerKCState
from app.rag.retrieval import RetrievalHit
from app.schemas.assessment import ItemRead
from app.schemas.chat import CheckComponentRead, CheckResultRead

_CITATION_MARKER = re.compile(r"\[(\d+)\]")

GROUNDING_INSTRUCTION = (
    "When your answer draws on one of the numbered passages below, cite it inline immediately "
    'after the sentence that uses it, like this: "...as shown here [1]." Only cite a passage '
    "you actually used — never invent a number that isn't listed."
)


async def add_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    model: str | None = None,
    citations: list[dict] | None = None,
    check_result: CheckResultRead | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        model=model,
        citations=citations or [],
        # Dumped here rather than by each caller, so the two flows cannot store the same
        # report in two shapes.
        check_result=check_result.model_dump(mode="json") if check_result is not None else None,
    )
    session.add(message)
    await session.flush()
    return message


def format_grounding(hits: Sequence[RetrievalHit]) -> str | None:
    """Numbered passages for a system prompt, paired with ``GROUNDING_INSTRUCTION``.

    Returns ``None`` for empty ``hits`` — the caller omits the grounding section entirely
    rather than including an awkward empty block.
    """
    if not hits:
        return None
    passages = "\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
    # Fenced as data (S31): a passage is whatever someone uploaded, and an uploaded document
    # can contain a sentence addressed to the model.
    return f"{GROUNDING_INSTRUCTION}\n\n{as_untrusted('RETRIEVED PASSAGES', passages)}"


def extract_citations(reply: str, hits: Sequence[RetrievalHit]) -> list[dict]:
    """Map every ``[N]`` marker actually present in ``reply`` to the hit it names.

    Only returns markers the model wrote — never invents a citation for a hit it didn't cite —
    and silently ignores out-of-range numbers (a hallucinated ``[7]`` with only 3 hits given).
    Deduplicates by marker (a repeated ``[1]`` yields one citation entry, not two).
    """
    seen: dict[int, dict] = {}
    for match in _CITATION_MARKER.finditer(reply):
        marker = int(match.group(1))
        if marker in seen or not (1 <= marker <= len(hits)):
            continue
        hit = hits[marker - 1]
        seen[marker] = {
            "marker": marker,
            "chunk_id": str(hit.chunk_id),
            "source_id": str(hit.source_id),
        }
    return [seen[m] for m in sorted(seen)]


def to_chat_messages(history: Sequence[Message]) -> list[ChatMessage]:
    """Convert persisted turns into provider-agnostic chat messages."""
    return [
        ChatMessage(role=ChatRole(m.role), content=m.content)
        for m in history
        if m.role in (ChatRole.USER, ChatRole.ASSISTANT)
    ]


@dataclass(frozen=True)
class TurnEvent:
    """A streamed step of a conversation turn. The router maps these to SSE frames."""

    type: Literal["token", "error", "done", "awaiting_reply", "committed", "tool_call"]
    text: str = ""
    detail: str = ""
    message_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None  # None = the model has no known price
    # The session runner's practice item for the plan's active step, if any — set on "done"
    # for subject-scoped conversations only. See app.services.session_runner.next_item.
    item: ItemRead | None = None
    # Citations grounding this turn's reply, if any — set on "done" for subject-scoped
    # conversations only. See extract_citations above.
    citations: list[dict] = field(default_factory=list)
    # What happened to an answer the learner gave in conversation, when this turn graded one
    # (S15). Set on "done". The tutor's reply already reflects the grade; this is the part the
    # learner can check it against, because a reply is not a record.
    check_result: CheckResultRead | None = None


def build_check_result(
    *,
    item_id: uuid.UUID,
    result: GradeResult,
    priors: Mapping[uuid.UUID, Estimate],
    states: Sequence[LearnerKCState],
    kcs: Sequence[KC],
    prior_kinds: Mapping[uuid.UUID, Mapping[FailureKind, int]] | None = None,
) -> CheckResultRead:
    """The grade, in the form the learner can read (S15).

    Shared by every flow that grades an answer, for the same reason the teaching instruction is
    (``app.learning.feedback``): one mistake must not be described two ways depending on which
    button the learner pressed.

    A component the grader could not score separately carries ``None`` rather than the item's
    aggregate — copying the aggregate down would present one verdict as several measurements,
    which is the exact error S10 exists to stop. ``none`` and ``incomplete`` carry no diagnosis,
    because "we could not tell" and "it was fine" are different things to say to somebody about
    their own work (S09), and the grader's confidence is not carried at all: a language model's
    self-reported confidence is not calibrated, and a number implies it is.
    """
    posterior = {state.kc_id: state for state in states}
    components: list[CheckComponentRead] = []
    for kc in kcs:
        prior = priors.get(kc.id)
        state = posterior.get(kc.id)
        diagnosis = result.diagnoses.get(kc.id)
        actionable = diagnosis is not None and diagnosis.actionable
        components.append(
            CheckComponentRead(
                kc_id=kc.id,
                kc_name=kc.name,
                score=result.component_scores.get(kc.id),
                prior_ability=prior.ability if prior is not None else 0.0,
                ability=state.ability if state is not None else 0.0,
                uncertainty=state.uncertainty if state is not None else 1.0,
                failure_kind=diagnosis.kind.value if actionable and diagnosis else None,
                failure_detail=(
                    diagnosis.evidence if actionable and diagnosis and diagnosis.evidence else None
                ),
                # Counted *before* this attempt was recorded, so it reads as "times before
                # this one" — the same number the teaching instruction branched on.
                recurrence=(
                    (prior_kinds or {}).get(kc.id, {}).get(diagnosis.kind, 0)
                    if actionable and diagnosis
                    else None
                ),
            )
        )
    return CheckResultRead(
        item_id=item_id,
        score=result.score,
        correct=result.correct,
        components=components,
    )
