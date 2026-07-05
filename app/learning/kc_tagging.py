"""Per-chunk KC auto-tagging for ingested content (TECHNICAL_DESIGN §6, §7).

After a source is chunked, each chunk is tagged with the knowledge components (KCs) it
teaches, so retrieval can filter by KC and content assembly can gather a KC's supporting
evidence. The FAST model classifies a chunk against the candidate KCs **scoped to the source's
subject/topic** — a bounded, relevant set, never the whole graph. Candidates are offered as a
numbered list and the model replies with indices, which is far more robust than having it echo
UUIDs. Tagging is **best-effort**: an unparseable, empty, or hallucinated reply yields no tags
rather than failing the ingest. All model access is by role — no provider SDK, no model name.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.knowledge import KC, Topic
from app.models.source import Source

TAGGING_ROLE = ModelRole.FAST
"""KC tagging is a cheap classification task — it runs on the FAST tier (MASTERPLAN §7)."""

_SYSTEM_PROMPT = (
    "You label a passage with the knowledge components (KCs) it actually teaches. You are given "
    "a numbered list of candidate KCs and a passage. Choose only the KCs the passage directly "
    "teaches or assesses — usually zero to three; omit tangential mentions. Respond with ONLY a "
    'JSON object {"tags": [{"kc": <candidate number>, "confidence": <0.0-1.0>}]} and nothing '
    "else. If none apply, return an empty list."
)


@dataclass(frozen=True)
class KCCandidate:
    """A KC offered to the tagger. Decoupled from the ORM so the classifier is pure/testable."""

    id: uuid.UUID
    name: str
    description: str | None = None


@dataclass(frozen=True)
class KCTag:
    """A KC the tagger assigned to a chunk, with the model's confidence (0.0-1.0)."""

    kc_id: uuid.UUID
    confidence: float


async def tag_chunk(
    client: LLMClient,
    text: str,
    candidates: Sequence[KCCandidate],
    *,
    min_confidence: float = 0.5,
    max_tokens: int = 256,
) -> tuple[list[KCTag], Usage]:
    """Tag one chunk against ``candidates`` with the FAST model.

    Returns the surviving tags (confidence ≥ ``min_confidence``, de-duplicated keeping the
    highest confidence, in candidate order) plus the call's ``Usage``. No candidates ⇒ no model
    call. A malformed/hallucinated reply ⇒ no tags (best-effort; never fails the ingest).
    """
    if not candidates:
        return [], Usage()
    completion = await client.complete(
        TAGGING_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(text, candidates))],
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    return _parse_tags(completion.content, candidates, min_confidence), completion.usage


def _build_prompt(text: str, candidates: Sequence[KCCandidate]) -> str:
    catalog = "\n".join(
        f"{i}. {kc.name}{f' — {kc.description}' if kc.description else ''}"
        for i, kc in enumerate(candidates, start=1)
    )
    return f"Candidate KCs:\n{catalog}\n\nPassage:\n{text}"


def _parse_tags(
    content: str, candidates: Sequence[KCCandidate], min_confidence: float
) -> list[KCTag]:
    try:
        raw = json.loads(_extract_json(content))["tags"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    best: dict[uuid.UUID, float] = {}
    for entry in raw:
        try:
            index = int(entry["kc"])
            confidence = _clamp(float(entry.get("confidence", 1.0)))
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= index <= len(candidates) or confidence < min_confidence:
            continue
        kc_id = candidates[index - 1].id
        best[kc_id] = max(best.get(kc_id, 0.0), confidence)
    return [KCTag(kc_id=c.id, confidence=best[c.id]) for c in candidates if c.id in best]


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


async def load_candidate_kcs(session: AsyncSession, source: Source) -> list[KCCandidate]:
    """Candidate KCs for tagging ``source``'s chunks: scoped to its topic, else its subject.

    An unscoped source (no subject *and* no topic) yields no candidates, so its chunks aren't
    tagged — classifying against the entire knowledge graph would be noise, not signal. Ordered
    by (topic slug, KC slug) so the candidate numbering the model sees is stable.
    """
    if source.topic_id is None and source.subject_id is None:
        return []
    stmt = select(KC).join(Topic, KC.topic_id == Topic.id)
    if source.topic_id is not None:
        stmt = stmt.where(KC.topic_id == source.topic_id)
    else:
        stmt = stmt.where(Topic.subject_id == source.subject_id)
    stmt = stmt.order_by(Topic.slug, KC.slug)
    rows = (await session.scalars(stmt)).all()
    return [KCCandidate(id=kc.id, name=kc.name, description=kc.description) for kc in rows]
