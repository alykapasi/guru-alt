"""Deterministic auto-grading for objective items (TECHNICAL_DESIGN §7.6).

MCQ / cloze / fill-in-the-blank grade to a score in [0, 1] with no model call — cloze and
fill-blank allow partial credit (fraction of blanks correct). Open responses (short/long)
raise :class:`NotAutoGradable`; slice 4 grades those against a rubric with the SMART model.
Every grading path ultimately feeds the tracer one ``Observation``.
"""

from pydantic import BaseModel

from app.models.assessment import AUTO_GRADABLE, ItemType


class NotAutoGradable(ValueError):
    """The item type has no deterministic key — it needs rubric (or self) grading."""


class SelfGradeError(ValueError):
    """A flashcard self-rating was missing or malformed."""


_RATING_SCORE: dict[int, float] = {1: 0.2, 2: 0.5, 3: 0.8, 4: 1.0}
"""FSRS-style self rating (1=Again … 4=Easy) → a continuous score for the tracer."""


class GradeResult(BaseModel):
    """Outcome of grading one response: a partial-credit ``score`` plus replayable detail."""

    score: float
    correct: bool
    detail: dict


def auto_grade(item_type: ItemType, answer_key: dict, response: dict) -> GradeResult:
    """Grade ``response`` against ``answer_key`` for an objective ``item_type``."""
    if item_type not in AUTO_GRADABLE:
        raise NotAutoGradable(f"{item_type} items are not auto-gradable")
    if item_type is ItemType.MCQ:
        return _grade_mcq(answer_key, response)
    return _grade_blanks(answer_key, response)


def grade_flashcard(response: dict) -> GradeResult:
    """Grade a self-rated flashcard recall.

    Accepts an FSRS-style ``rating`` (1=Again … 4=Easy) or a ``recalled`` boolean; both map
    to a score that feeds the tracer (and, via the score, FSRS scheduling in slice 5).
    """
    rating = response.get("rating")
    if rating is None and "recalled" in response:
        rating = 3 if response["recalled"] else 1
    score = (
        _RATING_SCORE.get(rating)
        if isinstance(rating, int) and not isinstance(rating, bool)
        else None
    )
    if score is None:
        raise SelfGradeError("flashcard response needs 'rating' (1-4) or 'recalled' (bool)")
    return GradeResult(
        score=score, correct=score >= 0.75, detail={"rating": rating, "method": "self"}
    )


def _grade_mcq(answer_key: dict, response: dict) -> GradeResult:
    correct_choice = answer_key.get("correct")
    chosen = response.get("choice")
    correct = chosen is not None and chosen == correct_choice
    return GradeResult(
        score=1.0 if correct else 0.0,
        correct=correct,
        detail={"chosen": chosen, "correct_choice": correct_choice},
    )


def _grade_blanks(answer_key: dict, response: dict) -> GradeResult:
    key_blanks = answer_key.get("blanks") or []
    if not key_blanks:
        raise NotAutoGradable("item has no answer-key blanks")
    given = response.get("blanks") or []
    hits = []
    for i, accepted in enumerate(key_blanks):
        options = [accepted] if isinstance(accepted, str) else list(accepted)
        acceptable = {_norm(o) for o in options}
        answer = given[i] if i < len(given) else ""
        hits.append(_norm(answer) in acceptable)
    return GradeResult(
        score=sum(hits) / len(key_blanks),
        correct=all(hits),
        detail={"blanks": hits},
    )


def _norm(value: object) -> str:
    """Case- and whitespace-insensitive normalization for string matching."""
    return str(value).strip().casefold()
