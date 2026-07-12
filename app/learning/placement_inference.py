"""Infer rough per-KC starting levels from a learner's free-text background (placement).

Same shape as ``app/learning/kc_tagging.py``: candidate KCs offered as a numbered list (never
UUIDs), a JSON-only reply, tolerant best-effort parsing. Unlike tagging, there's no "none"
level — a KC the learner shows no evidence for is simply omitted and stays at the tracer's
default unseen prior (ability 0, wide uncertainty), which already means "unknown".
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

INFERENCE_ROLE = ModelRole.FAST
"""Placement inference is a cheap classification task — the light-test/FAST tier."""

Level = Literal["some", "strong"]

_SYSTEM_PROMPT = (
    "A learner just described their background before starting a subject. You are given a "
    "numbered list of candidate knowledge components and the learner's self-description. "
    "Estimate their starting level only for KCs their description gives real evidence for — "
    "do not guess for the rest. Respond with ONLY a JSON object "
    '{"levels": [{"kc": <candidate number>, "level": "some"|"strong", "confidence": '
    '<0.0-1.0>}]} and nothing else. "some" = has touched it before; "strong" = clearly '
    "confident/experienced with it. If nothing applies, return an empty list."
)


@dataclass(frozen=True)
class KCCandidate:
    """A KC offered to the inference call. Decoupled from the ORM so it's pure/testable."""

    id: uuid.UUID
    name: str
    description: str | None = None


@dataclass(frozen=True)
class InferredLevel:
    kc_id: uuid.UUID
    level: Level
    confidence: float


async def infer_levels(
    client: LLMClient,
    background: str,
    candidates: Sequence[KCCandidate],
    *,
    max_tokens: int = 512,
) -> tuple[list[InferredLevel], Usage]:
    """Infer starting levels for the KCs ``background`` gives real evidence for.

    No candidates ⇒ no model call. A malformed/hallucinated reply ⇒ no levels (best-effort;
    placement always degrades gracefully to "nothing inferred", never fails the request).
    """
    if not candidates:
        return [], Usage()
    completion = await client.complete(
        INFERENCE_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(background, candidates))],
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    return _parse_levels(completion.content, candidates), completion.usage


def _build_prompt(background: str, candidates: Sequence[KCCandidate]) -> str:
    catalog = "\n".join(
        f"{i}. {kc.name}{f' — {kc.description}' if kc.description else ''}"
        for i, kc in enumerate(candidates, start=1)
    )
    return f"Candidate KCs:\n{catalog}\n\nLearner's self-description:\n{background}"


def _parse_levels(content: str, candidates: Sequence[KCCandidate]) -> list[InferredLevel]:
    try:
        raw = json.loads(_extract_json(content))["levels"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    best: dict[uuid.UUID, InferredLevel] = {}
    for entry in raw:
        try:
            index = int(entry["kc"])
            raw_level = str(entry["level"])
            confidence = _clamp(float(entry.get("confidence", 1.0)))
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= index <= len(candidates) or raw_level not in ("some", "strong"):
            continue
        level: Level = "strong" if raw_level == "strong" else "some"
        kc_id = candidates[index - 1].id
        if kc_id not in best or confidence > best[kc_id].confidence:
            best[kc_id] = InferredLevel(kc_id=kc_id, level=level, confidence=confidence)
    return [best[c.id] for c in candidates if c.id in best]


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
