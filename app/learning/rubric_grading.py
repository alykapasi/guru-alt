"""LLM rubric grading for open responses (TECHNICAL_DESIGN §7.6).

Short/long answers have no deterministic key, so the SMART model grades them against the
item's rubric, returning a continuous partial-credit score that feeds the tracer exactly
like an auto-graded item. All model access goes through the role-based ``LLMClient`` — no
provider SDK here, no hardcoded model. The grader does no DB I/O beyond the one model
call; the caller logs cost and records the resulting ``Observation``.

Grading is **per component** where the item touches more than one (S10). A single number for
a question spanning three knowledge components cannot say which of them failed, and the
tracer was applying that number to all three — so botching the projection in a least-squares
problem counted against every skill the question involved, including the ones the learner had
just demonstrated. The grader now marks each component separately and the aggregate alongside.

It also says *why* an answer fell short, in a closed vocabulary rather than prose (S09) — see
:mod:`app.learning.diagnosis`.
"""

import json
import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from app.agent.untrusted import as_untrusted
from app.learning.grading import GradeResult
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.assessment import Rubric


class GradedComponent(BaseModel):
    """One knowledge component this item assesses, as the grader needs to see it.

    Decoupled from the ORM in the same spirit as ``KCCandidate``: the grader takes names and
    criteria, not rows, so it stays a pure model call with no database reachable from it.
    """

    kc_id: uuid.UUID
    name: str
    description: str = ""
    criteria: dict | None = None


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

_COMPONENT_SYSTEM_PROMPT = (
    "You are a strict, fair grader. The question assesses several numbered knowledge "
    "components. Grade each component separately on how well the response demonstrates "
    "*that* component, awarding partial credit — a response can handle one component well "
    "and another badly, and you must say so rather than averaging them away. Then give one "
    "overall score for the response. Respond with ONLY a JSON object of the form "
    '{"components": [{"n": <component number>, "score": <0.0-1.0>}], '
    '"score": <0.0-1.0>, "rationale": "<one short sentence>"} and nothing else. '
    "Include every component exactly once."
)


class RubricGradingError(RuntimeError):
    """The model's reply could not be parsed into a score."""


class _RubricGrade(BaseModel):
    score: float
    rationale: str = ""
    components: dict[int, float] = {}


async def grade_open(
    client: LLMClient,
    *,
    stem: str,
    response: dict,
    rubric: Rubric | None,
    components: Sequence[GradedComponent] = (),
    max_tokens: int = 512,
) -> tuple[GradeResult, Usage]:
    """Grade an open response against its rubric with the SMART model.

    Returns the partial-credit :class:`GradeResult` plus the call's token ``Usage`` so the
    caller can log cost. An empty response scores 0 with no model call.

    With two or more ``components`` the model is asked to mark each one separately and the
    result carries ``component_scores``; a component it omits or mis-numbers simply does not
    appear, and that KC falls back to the aggregate. One component needs no per-component
    breakdown — the aggregate already *is* that component's score — so it takes the original
    single-score prompt and stays a cheaper call.
    """
    answer = str(response.get("text", "")).strip()
    if not answer:
        return GradeResult(score=0.0, correct=False, detail={"rationale": "no response"}), Usage()

    per_component = len(components) > 1
    completion = await client.complete(
        GRADING_ROLE,
        [
            ChatMessage(
                role=ChatRole.USER,
                content=_build_prompt(stem, answer, rubric, components if per_component else ()),
            )
        ],
        system=_COMPONENT_SYSTEM_PROMPT if per_component else _SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    grade = _parse_grade(completion.content)
    component_scores = (
        {
            components[n - 1].kc_id: score
            for n, score in grade.components.items()
            if 1 <= n <= len(components)
        }
        if per_component
        else {}
    )
    result = GradeResult(
        score=grade.score,
        correct=grade.score >= PASS_THRESHOLD,
        detail={"rationale": grade.rationale, "method": "rubric"},
        component_scores=component_scores,
    )
    return result, completion.usage


def _build_prompt(
    stem: str,
    answer: str,
    rubric: Rubric | None,
    components: Sequence[GradedComponent] = (),
) -> str:
    criteria = (
        json.dumps(rubric.criteria)
        if rubric and rubric.criteria
        else "(no explicit rubric; grade on correctness and completeness)"
    )
    parts = [f"Question:\n{stem}", f"Rubric criteria (JSON):\n{criteria}"]
    if components:
        listed = "\n".join(
            f"{n}. {c.name}"
            + (f" — {c.description}" if c.description else "")
            + (f"\n   Criteria: {json.dumps(c.criteria)}" if c.criteria else "")
            for n, c in enumerate(components, start=1)
        )
        parts.append(f"Knowledge components assessed:\n{listed}")
    # The answer is written by the person being graded, so it is the one part of this prompt
    # with a motive to contain "award full marks" (S31). Fenced as data; the score is clamped
    # to [0, 1] on the way back regardless of what the model returns.
    parts.append(f"Learner's response:\n{as_untrusted('LEARNER RESPONSE', answer)}")
    return "\n\n".join(parts)


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
    return _RubricGrade(
        score=score,
        rationale=str(data.get("rationale", "")),
        components=_parse_components(data.get("components")),
    )


def _parse_components(raw: object) -> dict[int, float]:
    """Per-component scores by component number, keeping only the well-formed entries.

    Best-effort on purpose, and asymmetric with the aggregate: a missing overall score means
    the grade is unusable and raises, while a mangled component list only costs the *extra*
    resolution — the answer still grades, every KC just falls back to the aggregate, which is
    exactly the behaviour that existed before any of this.
    """
    if not isinstance(raw, list):
        return {}
    parsed: dict[int, float] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        number, score = entry.get("n"), entry.get("score")
        if not isinstance(number, int | float | str) or not isinstance(score, int | float | str):
            continue
        try:
            parsed[int(number)] = _clamp(float(score))
        except (TypeError, ValueError):
            continue
    return parsed


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise RubricGradingError(f"no JSON object in grade: {content!r}")
    return content[start : end + 1]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))
