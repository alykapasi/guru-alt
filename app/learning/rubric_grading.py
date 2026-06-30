"""LLM rubric grading for open responses (TECHNICAL_DESIGN §7.6).

Short/long answers have no deterministic key, so the SMART model grades them against the
item's rubric, returning a continuous partial-credit score that feeds the tracer exactly
like an auto-graded item. All model access goes through the role-based ``LLMClient`` — no
provider SDK here, no hardcoded model. The grader does no DB I/O beyond the one model
call; the caller logs cost and records the resulting ``Observation``.
"""

import json

from pydantic import BaseModel

from app.learning.grading import GradeResult
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.assessment import Rubric

GRADING_ROLE = ModelRole.SMART
"""Open-response grading runs on the SMART tier (resolved by the model-role registry)."""

PASS_THRESHOLD = 0.6
"""Score at/above which ``correct`` is reported — a coarse display flag only. The tracer
always consumes the continuous score, never this boolean."""

_SYSTEM_PROMPT = (
    "You are a strict, fair grader. Grade the learner's response against the question and "
    "rubric, awarding partial credit for partially-correct answers. Respond with ONLY a "
    'JSON object of the form {"score": <number between 0.0 and 1.0>, "rationale": '
    '"<one short sentence>"} and nothing else.'
)


class RubricGradingError(RuntimeError):
    """The model's reply could not be parsed into a score."""


class _RubricGrade(BaseModel):
    score: float
    rationale: str = ""


async def grade_open(
    client: LLMClient,
    *,
    stem: str,
    response: dict,
    rubric: Rubric | None,
    max_tokens: int = 512,
) -> tuple[GradeResult, Usage]:
    """Grade an open response against its rubric with the SMART model.

    Returns the partial-credit :class:`GradeResult` plus the call's token ``Usage`` so the
    caller can log cost. An empty response scores 0 with no model call.
    """
    answer = str(response.get("text", "")).strip()
    if not answer:
        return GradeResult(score=0.0, correct=False, detail={"rationale": "no response"}), Usage()

    completion = await client.complete(
        GRADING_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(stem, answer, rubric))],
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    grade = _parse_grade(completion.content)
    result = GradeResult(
        score=grade.score,
        correct=grade.score >= PASS_THRESHOLD,
        detail={"rationale": grade.rationale, "method": "rubric"},
    )
    return result, completion.usage


def _build_prompt(stem: str, answer: str, rubric: Rubric | None) -> str:
    criteria = (
        json.dumps(rubric.criteria)
        if rubric and rubric.criteria
        else "(no explicit rubric; grade on correctness and completeness)"
    )
    return (
        f"Question:\n{stem}\n\nRubric criteria (JSON):\n{criteria}\n\nLearner's response:\n{answer}"
    )


def _parse_grade(content: str) -> _RubricGrade:
    """Extract and validate the JSON grade, tolerating prose around the object."""
    try:
        data = json.loads(_extract_json(content))
    except json.JSONDecodeError as err:
        raise RubricGradingError(f"unparseable grade: {content!r}") from err
    try:
        score = _clamp(float(data["score"]))
    except (KeyError, TypeError, ValueError) as err:
        raise RubricGradingError(f"grade has no valid score: {content!r}") from err
    return _RubricGrade(score=score, rationale=str(data.get("rationale", "")))


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise RubricGradingError(f"no JSON object in grade: {content!r}")
    return content[start : end + 1]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
