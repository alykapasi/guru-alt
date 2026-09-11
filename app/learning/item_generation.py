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

Every generator takes a ``target_difficulty`` and records it on the item it writes (S12).
Read that number for exactly what it is: **the level we asked for, not a property we measured**.
Nothing here checks that the model delivered it, and an item's difficulty is only really known
once learners of known ability have answered it.

Recording the request anyway is the lesser of two dishonesties. The alternative was the status
quo, where every generated item took the ``difficulty`` column default of 0.0 — and since
generation is how items come to exist in practice, that default was the entire scale. It made
the tracer score every question as if pitched at the population average, and it made the
``optimal_challenge`` profile dimension the mean of a column of zeros. A stored 0.0 is not a
missing value; it is a specific and unearned claim. The request at least lands on the right
scale and moves with the learner.
"""

import json
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import difficulty as difficulty_mod
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.assessment import Item, ItemType, Rubric
from app.models.knowledge import KC
from app.schemas.assessment import ItemCreate, ItemKCRef
from app.services import assessment as assessment_svc

GENERATION_ROLE = ModelRole.FAST
"""Item generation is a short, low-stakes, one-shot task — the light-test/FAST tier."""


def _pitch(target_difficulty: float | None) -> str:
    """The sentence that tells the model how hard to make it, or nothing.

    A band, never the number. ``-0.35`` is not something a model can aim at, and asking one to
    calibrate its own output to a logit invites a confident guess — see
    ``app.learning.difficulty``.
    """
    if target_difficulty is None:
        return ""
    return f" Pitch the question at this level: {difficulty_mod.describe(target_difficulty)}."


def _recorded_difficulty(target_difficulty: float | None) -> float:
    """What lands in the item's ``difficulty`` column. See the module docstring for what that
    number does and does not claim; ``None`` keeps the old uncalibrated 0.0."""
    return 0.0 if target_difficulty is None else target_difficulty


_SYSTEM_PROMPT = (
    "You write one short multiple-choice question that tests understanding of a single "
    "knowledge component. Respond with ONLY a JSON object "
    '{"stem": "<question>", "choices": ["<option>", ...], "correct": <0-based index>} and '
    "nothing else. Provide exactly 4 choices, plausible but unambiguous, with exactly one "
    "correct answer."
)


async def generate_mcq_item(
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    target_difficulty: float | None = None,
    max_tokens: int = 256,
) -> tuple[Item | None, Usage]:
    """Generate and persist one MCQ item for ``kc``, or ``(None, usage)`` on a bad reply."""
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_SYSTEM_PROMPT + _pitch(target_difficulty),
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
            difficulty=_recorded_difficulty(target_difficulty),
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
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    target_difficulty: float | None = None,
    max_tokens: int = 256,
) -> tuple[Item | None, Usage]:
    """Generate and persist one fill-in-the-blank item for ``kc``, or ``(None, usage)``."""
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_FILL_BLANK_SYSTEM_PROMPT + _pitch(target_difficulty),
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
            difficulty=_recorded_difficulty(target_difficulty),
        ),
    )
    return item, completion.usage


_SHORT_SYSTEM_PROMPT = (
    "You write one short-answer question that requires a brief written explanation to test "
    "understanding of a single knowledge component, together with the criteria a grader "
    "should mark it against. Respond with ONLY a JSON object "
    '{"stem": "<the question>", "criteria": ["<what a full-credit answer must show>", ...]} '
    "and nothing else. Give two to four criteria, each one specific and checkable."
)


async def generate_short_item(
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    target_difficulty: float | None = None,
    max_tokens: int = 256,
) -> tuple[Item | None, Usage]:
    """Generate and persist one open, rubric-graded short-answer item for ``kc``, with the
    criteria it should be graded against (S10).

    The question and its marking criteria are written in the same call, by the model that
    knows what it was asking for. Before this nothing in the system produced a ``Rubric`` row
    at all — the table existed, `Item.rubric_id` was always null, and every open answer was
    graded against `grade_open`'s "(no explicit rubric; grade on correctness and
    completeness)" fallback. An open question with no stated standard is graded to whatever
    standard the grader improvises on the day, which is not a standard.

    Criteria are best-effort: a reply without usable ones still yields the item, graded the
    old way. A question is worth more than no question.

    No ``answer_key`` — ``ItemCreate`` only requires one for auto-gradable types.
    """
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_SHORT_SYSTEM_PROMPT + _pitch(target_difficulty),
        max_tokens=max_tokens,
    )
    parsed = _parse_short(completion.content)
    if parsed is None:
        return None, completion.usage
    stem, criteria = parsed
    rubric_id = None
    if criteria:
        rubric = Rubric(
            kc_id=kc.id,
            name=f"Generated criteria for {kc.name}"[:255],
            criteria={"criteria": criteria},
        )
        session.add(rubric)
        await session.flush()
        rubric_id = rubric.id
    item = await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.SHORT,
            stem=stem,
            kcs=[ItemKCRef(kc_id=kc.id)],
            difficulty=_recorded_difficulty(target_difficulty),
            rubric_id=rubric_id,
        ),
    )
    return item, completion.usage


_FLASHCARD_SYSTEM_PROMPT = (
    "You write one flashcard question that tests recall of a single knowledge component. "
    'Respond with ONLY a JSON object {"stem": "<a short question testing recall>", "answer": '
    '"<the answer>"} and nothing else.'
)


async def generate_flashcard_item(
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    target_difficulty: float | None = None,
    max_tokens: int = 256,
) -> tuple[Item | None, Usage]:
    """Generate and persist one flashcard item for ``kc``, or ``(None, usage)``.

    The learner self-rates recall (``grading.grade_flashcard``) — the generated ``answer`` is
    stored but not otherwise consumed yet (see module docstring).
    """
    completion = await llm.complete(
        GENERATION_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(kc))],
        system=_FLASHCARD_SYSTEM_PROMPT + _pitch(target_difficulty),
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
            difficulty=_recorded_difficulty(target_difficulty),
        ),
    )
    return item, completion.usage


@runtime_checkable
class GeneratorFn(Protocol):
    """What every generator above looks like to a caller dispatching on item type.

    A Protocol rather than a ``Callable`` alias because ``target_difficulty`` is keyword-only
    and ``Callable[...]`` has no way to say so — it would have silently typed the dispatch as
    taking three positional arguments and nothing else. ``runtime_checkable`` because beartype
    enforces the ``GENERATORS`` annotation at import time and cannot check a plain Protocol.
    """

    async def __call__(
        self,
        session: AsyncSession,
        llm: LLMClient,
        kc: KC,
        *,
        target_difficulty: float | None = None,
    ) -> tuple[Item | None, Usage]: ...


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


def _parse_short(content: str) -> tuple[str, list[str]] | None:
    """``(stem, criteria)``. Criteria may be empty; a missing stem is what makes it unusable."""
    try:
        raw = json.loads(_extract_json(content))
        stem = str(raw["stem"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if not stem:
        return None
    listed = raw.get("criteria")
    criteria = (
        [text for text in (str(c).strip() for c in listed) if text]
        if isinstance(listed, list)
        else []
    )
    return stem, criteria


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
