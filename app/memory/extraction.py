"""LLM extraction of durable per-learner memories from a conversation transcript.

Mirrors ``app/learning/kc_tagging.py``'s shape: a cheap FAST-role call, a JSON-only prompt,
tolerant best-effort parsing (an unparseable reply or one with no usable entries yields no
memories rather than failing the caller). Extracts facts about the LEARNER — personal context,
stated preferences, and a short summary of what was covered — never the subject matter being
taught, which belongs to the knowledge graph / content, not memory.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.llm.types import text_of
from app.models.memory import MemoryKind

EXTRACTION_ROLE = ModelRole.FAST
"""Memory extraction is a short, low-stakes, one-shot task — the light/tagging tier."""

MAX_MEMORIES = 5
"""Hard cap on extracted items per call, enforced even if the model ignores the prompt's ask."""

_SYSTEM_PROMPT = (
    "You read a tutoring conversation and extract durable facts about the LEARNER — not the "
    "subject matter being taught. Look for: personal context, stated preferences, goals, and a "
    "short summary of what was covered. Respond with ONLY a JSON object "
    '{"memories": [{"kind": "fact"|"preference"|"summary", "content": "<short sentence>"}]} '
    f"and nothing else. Extract at most {MAX_MEMORIES} distinct items. If nothing durable is "
    "worth remembering, return an empty list."
)


@dataclass(frozen=True)
class ExtractedMemory:
    """One candidate memory pulled from a transcript, not yet embedded or persisted."""

    kind: MemoryKind
    content: str


async def extract_memories(
    llm: LLMClient, messages: Sequence[ChatMessage], *, max_tokens: int = 512
) -> tuple[list[ExtractedMemory], Usage]:
    """Extract durable memories from ``messages`` (a conversation's user/assistant turns).

    No messages ⇒ no model call. A malformed/hallucinated reply ⇒ no memories (best-effort;
    never fails the caller).
    """
    if not messages:
        return [], Usage()
    completion = await llm.complete(
        EXTRACTION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(messages))],
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    return _parse_memories(completion.content), completion.usage


def _build_prompt(messages: Sequence[ChatMessage]) -> str:
    transcript = "\n".join(f"{m.role.capitalize()}: {text_of(m.content)}" for m in messages)
    return f"Conversation:\n{transcript}"


def _parse_memories(content: str) -> list[ExtractedMemory]:
    try:
        raw = json.loads(_extract_json(content))["memories"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    extracted: list[ExtractedMemory] = []
    for entry in raw:
        try:
            kind = MemoryKind(entry["kind"])
            text = str(entry["content"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not text:
            continue
        extracted.append(ExtractedMemory(kind=kind, content=text))
        if len(extracted) >= MAX_MEMORIES:
            break
    return extracted


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]
