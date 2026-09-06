"""Per-chunk KC auto-tagging for ingested content (TECHNICAL_DESIGN §6, §7).

After a source is chunked, each chunk is tagged with the knowledge components (KCs) it
teaches, so retrieval can filter by KC and content assembly can gather a KC's supporting
evidence. The FAST model classifies a chunk against the candidate KCs **scoped to the source's
subject/topic** — a bounded, relevant set, never the whole graph. Candidates are offered as a
numbered list and the model replies with indices, which is far more robust than having it echo
UUIDs. Tagging is **best-effort**: an unparseable, empty, or hallucinated reply yields no tags
rather than failing the ingest. All model access is by role — no provider SDK, no model name.

The classification itself runs as a DSPy program (app/prompts/kc_tagging_program.py), reached
through ``RoleLM`` so the model is still selected by role, never a provider SDK or model name
(Phase 9c, D8). This module owns the candidate-catalog framing and the tag/threshold/dedup
mapping back to ``KCTag``; the instruction text and few-shot demos live in the DSPy program.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient, ModelRole, Usage
from app.models.knowledge import KC, Topic
from app.models.source import Source
from app.prompts.kc_tagging_program import TagPrediction, load_kc_tagging_program
from app.prompts.lm import RoleLM

TAGGING_ROLE = ModelRole.FAST
"""KC tagging is a cheap classification task — it runs on the FAST tier (MASTERPLAN §7)."""


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
    """Tag one chunk against ``candidates`` with the FAST model via the DSPy program.

    Best-effort: no candidates ⇒ no call; any DSPy/exec/parse failure ⇒ no tags (never raises).
    Returns surviving tags (confidence ≥ ``min_confidence``, de-duped keeping the highest, in
    candidate order) plus the call's ``Usage`` for cost logging.
    """
    if not candidates:
        return [], Usage()
    lm = RoleLM(TAGGING_ROLE, client, max_tokens=max_tokens)
    catalog = "\n".join(
        f"{i}. {c.name}{f' — {c.description}' if c.description else ''}"
        for i, c in enumerate(candidates, start=1)
    )
    program = load_kc_tagging_program()
    try:
        import dspy

        with dspy.context(lm=lm):
            prediction = await program.acall(passage=text, candidates=catalog)
        raw = list(prediction.tags)
    except Exception:  # best-effort — a weak model/parse failure never fails the ingest
        return [], lm.usage_sum
    return _surviving_tags(raw, candidates, min_confidence), lm.usage_sum


def _surviving_tags(
    raw: Sequence[TagPrediction], candidates: Sequence[KCCandidate], min_confidence: float
) -> list[KCTag]:
    best: dict[uuid.UUID, float] = {}
    for entry in raw:
        try:
            index = int(entry.kc)
            confidence = _clamp(float(entry.confidence))
        except (AttributeError, TypeError, ValueError):
            continue
        if not 1 <= index <= len(candidates) or confidence < min_confidence:
            continue
        kc_id = candidates[index - 1].id
        best[kc_id] = max(best.get(kc_id, 0.0), confidence)
    return [KCTag(kc_id=c.id, confidence=best[c.id]) for c in candidates if c.id in best]


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
