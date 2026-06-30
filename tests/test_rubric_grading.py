"""Unit tests for LLM rubric grading (offline; FakeProvider returns a canned JSON grade)."""

import pytest

from app.learning.grading import GradeResult
from app.learning.rubric_grading import RubricGradingError, grade_open
from app.llm.registry import fake_llm_client


def _client(reply: str):
    return fake_llm_client(reply=reply)


async def test_grade_open_parses_score_and_rationale() -> None:
    client = _client('{"score": 0.8, "rationale": "Mostly right."}')
    result, usage = await grade_open(
        client, stem="Explain X", response={"text": "an answer"}, rubric=None
    )
    assert isinstance(result, GradeResult)
    assert result.score == 0.8
    assert result.correct is True  # >= PASS_THRESHOLD
    assert result.detail["rationale"] == "Mostly right."
    assert result.detail["method"] == "rubric"
    assert usage.output_tokens > 0


async def test_grade_open_tolerates_surrounding_prose() -> None:
    client = _client('Here is the grade: {"score": 0.4, "rationale": "Partial."} Done.')
    result, _ = await grade_open(client, stem="Q", response={"text": "a"}, rubric=None)
    assert result.score == 0.4
    assert result.correct is False  # below PASS_THRESHOLD


async def test_grade_open_clamps_out_of_range_score() -> None:
    client = _client('{"score": 1.7, "rationale": "Excellent."}')
    result, _ = await grade_open(client, stem="Q", response={"text": "a"}, rubric=None)
    assert result.score == 1.0


async def test_grade_open_empty_response_skips_model() -> None:
    # Invalid JSON would raise if the model were consulted; the empty-response guard skips it.
    client = _client("this is not valid json")
    result, usage = await grade_open(client, stem="Q", response={"text": "   "}, rubric=None)
    assert result.score == 0.0
    assert result.correct is False
    assert usage.input_tokens == 0 and usage.output_tokens == 0


async def test_grade_open_raises_on_unparseable_reply() -> None:
    client = _client("no json object here at all")
    with pytest.raises(RubricGradingError):
        await grade_open(client, stem="Q", response={"text": "a real answer"}, rubric=None)


async def test_grade_open_raises_when_score_missing() -> None:
    client = _client('{"rationale": "forgot the score"}')
    with pytest.raises(RubricGradingError):
        await grade_open(client, stem="Q", response={"text": "a real answer"}, rubric=None)
