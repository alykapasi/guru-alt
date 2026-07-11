"""LLM-generated MCQ items for KCs that have no item bank yet (placement's light test).

Mirrors ``app/learning/kc_tagging.py``'s shape: a cheap FAST-role call, a JSON-only prompt,
tolerant best-effort parsing (an unparseable or invalid reply yields no item rather than
failing the caller). Generated items are ordinary, learner-unscoped ``Item`` rows — once
created for a KC they join the shared bank permanently, so later placements/lessons reuse
them instead of paying to regenerate.
"""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.assessment import Item, ItemType
from app.models.knowledge import KC
from app.schemas.assessment import ItemCreate, ItemKCRef
from app.services import assessment as assessment_svc

GENERATION_ROLE = ModelRole.FAST
"""Item generation is a short, low-stakes, one-shot task — the light-test/FAST tier."""

_SYSTEM_PROMPT = (
    "You write one short multiple-choice question that tests understanding of a single "
    "knowledge component. Respond with ONLY a JSON object "
    '{"stem": "<question>", "choices": ["<option>", ...], "correct": <0-based index>} and '
    "nothing else. Provide exactly 4 choices, plausible but unambiguous, with exactly one "
    "correct answer."
)


async def generate_mcq_item(
    session: AsyncSession, llm: LLMClient, kc: KC, *, max_tokens: int = 256
) -> tuple[Item | None, Usage]:
    """Generate and persist one MCQ item for ``kc``, or ``(None, usage)`` on a bad reply."""
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    parsed = _parse_mcq(completion.content)
    if parsed is None:
        return None, completion.usage
    stem, choices, correct = parsed
    item = await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.MCQ,
            stem=stem,
            kcs=[ItemKCRef(kc_id=kc.id)],
            answer_key={"choices": choices, "correct": correct},
        ),
    )
    return item, completion.usage


def _build_prompt(kc: KC) -> str:
    return f"Knowledge component: {kc.name}" + (f"\n{kc.description}" if kc.description else "")


def _parse_mcq(content: str) -> tuple[str, list[str], int] | None:
    try:
        raw = json.loads(_extract_json(content))
        stem = str(raw["stem"]).strip()
        choices = [str(c).strip() for c in raw["choices"]]
        correct = int(raw["correct"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not stem or len(choices) < 2 or not (0 <= correct < len(choices)):
        return None
    return stem, choices, correct


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]
