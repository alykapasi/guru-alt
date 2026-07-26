"""Shared helpers for streamed conversation turns (plain tutoring + the refinement gate).

Split out from ``chat.py`` so ``refinement.py`` can reuse them without the two service
modules importing each other.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.types import ChatMessage, ChatRole, Usage
from app.models.chat import LLMCall, Message
from app.rag.retrieval import RetrievalHit
from app.schemas.assessment import ItemRead

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
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        model=model,
        citations=citations or [],
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
    return f"{GROUNDING_INSTRUCTION}\n\n{passages}"


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


async def record_llm_call(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    role: str,
    provider: str,
    model: str,
    usage: Usage,
    cost_usd: float,
) -> LLMCall:
    call = LLMCall(
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=role,
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost_usd,
    )
    session.add(call)
    await session.flush()
    return call


@dataclass(frozen=True)
class TurnEvent:
    """A streamed step of a conversation turn. The router maps these to SSE frames."""

    type: Literal["token", "error", "done", "awaiting_reply", "committed", "tool_call"]
    text: str = ""
    detail: str = ""
    message_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    # The session runner's practice item for the plan's active step, if any — set on "done"
    # for subject-scoped conversations only. See app.services.session_runner.next_item.
    item: ItemRead | None = None
    # Citations grounding this turn's reply, if any — set on "done" for subject-scoped
    # conversations only. See extract_citations above.
    citations: list[dict] = field(default_factory=list)
