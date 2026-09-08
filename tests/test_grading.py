"""Unit tests for the deterministic auto-grader (pure, no DB)."""

import pytest

from app.learning.grading import (
    InvalidResponse,
    NotAutoGradable,
    SelfGradeError,
    auto_grade,
    grade_flashcard,
)
from app.models.assessment import ItemType


def test_mcq_correct_and_wrong() -> None:
    key = {"choices": ["a", "b", "c"], "correct": 1}
    assert auto_grade(ItemType.MCQ, key, {"choice": 1}).score == 1.0
    wrong = auto_grade(ItemType.MCQ, key, {"choice": 0})
    assert wrong.score == 0.0
    assert wrong.correct is False
    assert wrong.detail == {"chosen": 0, "correct_choice": 1}


def test_mcq_no_answer_is_rejected_not_scored_zero() -> None:
    """Changed by S54: an unanswered item must not become evidence of not knowing it.

    Previously this scored 0.0, which fed the tracer a wrong-answer observation for a
    question the learner never actually answered.
    """
    with pytest.raises(InvalidResponse):
        auto_grade(ItemType.MCQ, {"correct": 1}, {})


def test_cloze_partial_credit() -> None:
    key = {"blanks": ["mitochondria", "ATP", "respiration"]}
    result = auto_grade(ItemType.CLOZE, key, {"blanks": ["mitochondria", "ATP", "wrong"]})
    assert result.score == pytest.approx(2 / 3)
    assert result.correct is False
    assert result.detail["blanks"] == [True, True, False]


def test_cloze_normalizes_case_and_whitespace() -> None:
    key = {"blanks": ["Mitochondria"]}
    assert auto_grade(ItemType.CLOZE, key, {"blanks": ["  mitochondria "]}).score == 1.0


def test_blank_accepts_any_listed_alternative() -> None:
    key = {"blanks": [["color", "colour"]]}
    assert auto_grade(ItemType.FILL_BLANK, key, {"blanks": ["colour"]}).score == 1.0
    assert auto_grade(ItemType.FILL_BLANK, key, {"blanks": ["hue"]}).score == 0.0


def test_missing_blanks_count_as_wrong() -> None:
    key = {"blanks": ["a", "b"]}
    result = auto_grade(ItemType.FILL_BLANK, key, {"blanks": ["a"]})
    assert result.score == 0.5


def test_open_items_are_not_auto_gradable() -> None:
    for item_type in (ItemType.SHORT, ItemType.LONG):
        with pytest.raises(NotAutoGradable):
            auto_grade(item_type, {}, {"text": "an essay"})


def test_flashcard_rating_maps_to_score() -> None:
    assert grade_flashcard({"rating": 1}).score == 0.2
    assert grade_flashcard({"rating": 3}).score == 0.8
    easy = grade_flashcard({"rating": 4})
    assert easy.score == 1.0
    assert easy.correct is True
    assert easy.detail["method"] == "self"


def test_flashcard_recalled_boolean() -> None:
    assert grade_flashcard({"recalled": True}).score == 0.8  # → Good
    assert grade_flashcard({"recalled": False}).score == 0.2  # → Again


def test_flashcard_bad_rating_raises() -> None:
    for bad in ({"rating": 5}, {"rating": "good"}, {}):
        with pytest.raises(SelfGradeError):
            grade_flashcard(bad)


# --- response validation: bad input is not a wrong answer --------------------


def test_mcq_missing_choice_raises_rather_than_scoring_zero() -> None:
    key = {"choices": ["a", "b"], "correct": 1}
    with pytest.raises(InvalidResponse):
        auto_grade(ItemType.MCQ, key, {})


def test_mcq_non_integer_choice_raises() -> None:
    key = {"choices": ["a", "b"], "correct": 1}
    with pytest.raises(InvalidResponse):
        auto_grade(ItemType.MCQ, key, {"choice": "b"})


def test_mcq_out_of_range_choice_raises() -> None:
    key = {"choices": ["a", "b"], "correct": 1}
    with pytest.raises(InvalidResponse):
        auto_grade(ItemType.MCQ, key, {"choice": 7})


def test_blanks_missing_key_raises() -> None:
    with pytest.raises(InvalidResponse):
        auto_grade(ItemType.CLOZE, {"blanks": ["a"]}, {})


def test_short_blanks_list_still_grades_partially() -> None:
    """A too-short answer is a real (partial) attempt, not malformed input."""
    result = auto_grade(ItemType.FILL_BLANK, {"blanks": ["a", "b"]}, {"blanks": ["a"]})
    assert result.score == 0.5
