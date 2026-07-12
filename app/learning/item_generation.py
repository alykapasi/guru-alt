"""LLM-generated items for KCs that have no item bank yet (placement's light test, and the
session runner's type-aware resolution — see ``app.services.session_runner``).

Mirrors ``app/learning/kc_tagging.py``'s shape: a cheap FAST-role call, a JSON-only prompt,
tolerant best-effort parsing (an unparseable or invalid reply yields no item rather than
failing the caller). Generated items are ordinary, learner-unscoped ``Item`` rows — once
created for a KC they join the shared bank permanently, so later placements/lessons reuse
them instead of paying to regenerate.

Flashcard generation captures the model's answer into ``answer_key={"back": ...}`` even
though nothing reads it today — self-rated grading (``grading.grade_flashcard``) trusts the
learner's rating completely, with no "reveal the answer before you self-rate" UX yet. Storing
it anyway is forward-compatible plumbing (the answer already exists in the reply; discarding
it is pure waste) rather than a fix for that gap.

Cloze has no generator here (only fill-in-the-blank does) — reuse-from-bank still works for
cloze via ``assessment.find_item_for_kc``'s type filter, generation is a documented future gap.
"""

import json
from collections.abc import Awaitable, Callable

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


_FILL_BLANK_SYSTEM_PROMPT = (
    "You write one fill-in-the-blank question that tests understanding of a single knowledge "
    'component. Respond with ONLY a JSON object {"stem": "<sentence with exactly one blank '
    'written as ___>", "answer": "<the word or phrase that belongs in the blank>"} and nothing '
    "else. The stem must contain the literal characters ___ exactly once."
)


async def generate_fill_blank_item(
    session: AsyncSession, llm: LLMClient, kc: KC, *, max_tokens: int = 256
) -> tuple[Item | None, Usage]:
    """Generate and persist one fill-in-the-blank item for ``kc``, or ``(None, usage)``."""
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_FILL_BLANK_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    parsed = _parse_fill_blank(completion.content)
    if parsed is None:
        return None, completion.usage
    stem, answer = parsed
    item = await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.FILL_BLANK,
            stem=stem,
            kcs=[ItemKCRef(kc_id=kc.id)],
            answer_key={"blanks": [answer]},
        ),
    )
    return item, completion.usage


_SHORT_SYSTEM_PROMPT = (
    "You write one short-answer question that requires a brief written explanation to test "
    "understanding of a single knowledge component. Respond with ONLY a JSON object "
    '{"stem": "<the question>"} and nothing else.'
)


async def generate_short_item(
    session: AsyncSession, llm: LLMClient, kc: KC, *, max_tokens: int = 256
) -> tuple[Item | None, Usage]:
    """Generate and persist one open, rubric-graded short-answer item for ``kc``.

    No ``answer_key``/``rubric_id`` — ``rubric_grading.grade_open`` grades on correctness and
    completeness when there's no explicit rubric, and ``ItemCreate`` only requires an
    ``answer_key`` for auto-gradable types.
    """
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_SHORT_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    stem = _parse_short(completion.content)
    if stem is None:
        return None, completion.usage
    item = await assessment_svc.create_item(
        session,
        ItemCreate(item_type=ItemType.SHORT, stem=stem, kcs=[ItemKCRef(kc_id=kc.id)]),
    )
    return item, completion.usage


_FLASHCARD_SYSTEM_PROMPT = (
    "You write one flashcard question that tests recall of a single knowledge component. "
    'Respond with ONLY a JSON object {"stem": "<a short question testing recall>", "answer": '
    '"<the answer>"} and nothing else.'
)


async def generate_flashcard_item(
    session: AsyncSession, llm: LLMClient, kc: KC, *, max_tokens: int = 256
) -> tuple[Item | None, Usage]:
    """Generate and persist one flashcard item for ``kc``, or ``(None, usage)``.

    The learner self-rates recall (``grading.grade_flashcard``) — the generated ``answer`` is
    stored but not otherwise consumed yet (see module docstring).
    """
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_FLASHCARD_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    parsed = _parse_flashcard(completion.content)
    if parsed is None:
        return None, completion.usage
    stem, answer = parsed
    item = await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.FLASHCARD,
            stem=stem,
            kcs=[ItemKCRef(kc_id=kc.id)],
            answer_key={"back": answer} if answer else None,
        ),
    )
    return item, completion.usage


GeneratorFn = Callable[[AsyncSession, LLMClient, KC], Awaitable[tuple[Item | None, Usage]]]

GENERATORS: dict[ItemType, GeneratorFn] = {
    ItemType.MCQ: generate_mcq_item,
    ItemType.FILL_BLANK: generate_fill_blank_item,
    ItemType.SHORT: generate_short_item,
    ItemType.FLASHCARD: generate_flashcard_item,
}
"""Every item type this module can generate — what lets callers (the session runner) dispatch
on a preferred type generically instead of hardcoding one generator."""


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


def _parse_fill_blank(content: str) -> tuple[str, str] | None:
    try:
        raw = json.loads(_extract_json(content))
        stem = str(raw["stem"]).strip()
        answer = str(raw["answer"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not stem or "___" not in stem or not answer:
        return None
    return stem, answer


def _parse_short(content: str) -> str | None:
    try:
        raw = json.loads(_extract_json(content))
        stem = str(raw["stem"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return stem or None


def _parse_flashcard(content: str) -> tuple[str, str] | None:
    try:
        raw = json.loads(_extract_json(content))
        stem = str(raw["stem"]).strip()
        answer = str(raw.get("answer", "")).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not stem:
        return None
    return stem, answer


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]
