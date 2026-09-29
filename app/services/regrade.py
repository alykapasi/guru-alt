"""Put past answers through a grader again, and say how far it agrees (S56).

Read-only by construction: nothing here writes a score, an event or a mastery state. It
selects graded attempts that carry a ``grading`` block (event schema v5), rebuilds what the
grader was given from the learner's snapshots, and compares. Calls are attributed to feature
``regrade`` with no learner, so they count against the deployment ceiling and never against a
learner's own cap. See docs/RUNBOOK.md §19.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.grading import GradeResult, auto_grade
from app.learning.rubric_grading import GRADING_ROLE, GradedComponent, grade_open
from app.llm import LLMClient
from app.llm.attribution import attributed
from app.llm.pricing import price_usd
from app.llm.types import Usage
from app.models.assessment import ItemType, Rubric
from app.models.grading import GradingSnapshot
from app.models.knowledge import KC, Topic
from app.models.learning import LearningEvent

GRADED_EVENTS = ("observation", "admin_observation")
LARGEST = 10


@dataclass(frozen=True)
class Candidate:
    event_id: uuid.UUID
    learner_id: uuid.UUID
    score: float
    correct: bool
    component_scores: dict[str, float]
    response: dict
    grading: dict
    item: dict
    rubric: dict | None
    prompt: dict | None


@dataclass(frozen=True)
class Plan:
    candidates: list[Candidate]
    not_regradable: int
    by_grader: dict[str, int]


@dataclass(frozen=True)
class Comparison:
    event_id: uuid.UUID
    recorded: float
    regraded: float


@dataclass(frozen=True)
class Agreement:
    compared: int
    correct_agreement: float | None
    mean_abs_score_diff: float | None


@dataclass(frozen=True)
class Report:
    compared: int
    failed: int
    correct_agreement: float | None
    mean_abs_score_diff: float | None
    component_mean_abs_diff: float | None
    largest: list[Comparison]
    by_grader: dict[str, Agreement]
    """The same numbers per recorded grader: free auto re-grades almost always agree, so pooled
    with the model's they would hide the disagreement a prompt or model change is measured by."""


async def plan(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    learner_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 200,
) -> Plan:
    query = (
        select(LearningEvent)
        .where(
            LearningEvent.event_type.in_(GRADED_EVENTS),
            LearningEvent.created_at >= since,
            LearningEvent.created_at < until,
        )
        .order_by(LearningEvent.created_at.desc(), LearningEvent.id)
    )
    if learner_id is not None:
        query = query.where(LearningEvent.learner_id == learner_id)
    if subject_id is not None:
        query = query.where(
            LearningEvent.kc_id.in_(
                select(KC.id)
                .join(Topic, KC.topic_id == Topic.id)
                .where(Topic.subject_id == subject_id)
            )
        )
    # One candidate per attempt: an answer's per-KC fan-out is one grade.
    attempts: dict[uuid.UUID, list[LearningEvent]] = {}
    for event in (await session.scalars(query)).all():
        key = event.attempt_id or event.id
        if key not in attempts and len(attempts) >= limit:
            continue
        attempts.setdefault(key, []).append(event)

    candidates: list[Candidate] = []
    not_regradable = 0
    by_grader: dict[str, int] = {}
    for events in attempts.values():
        first = events[0]
        grading = first.payload.get("grading")
        response = first.payload.get("response")
        if not grading or response is None or grading.get("grader") == "self":
            not_regradable += 1
            continue
        wanted = [h for h in (grading["item"], grading.get("rubric"), grading.get("prompt")) if h]
        found = {
            s.sha256: s.content
            for s in await session.scalars(
                select(GradingSnapshot).where(
                    GradingSnapshot.learner_id == first.learner_id,
                    GradingSnapshot.sha256.in_(wanted),
                )
            )
        }
        if any(h not in found for h in wanted):
            not_regradable += 1
            continue
        candidates.append(
            Candidate(
                event_id=first.id,
                learner_id=first.learner_id,
                score=float(first.payload.get("item_score", first.payload["score"])),
                correct=bool(first.payload.get("correct")),
                component_scores=(
                    {str(e.kc_id): float(e.payload["score"]) for e in events if e.kc_id}
                    if first.payload.get("component_scored")
                    else {}
                ),
                response=response,
                grading=grading,
                item=found[grading["item"]],
                rubric=found.get(grading.get("rubric") or ""),
                prompt=found.get(grading.get("prompt") or ""),
            )
        )
        by_grader[grading["grader"]] = by_grader.get(grading["grader"], 0) + 1
    return Plan(candidates, not_regradable, by_grader)


def _paid(candidate: Candidate) -> bool:
    return candidate.grading["grader"] in ("rubric", "jev")


def estimate_cost(found: Plan, llm: LLMClient) -> float | None:
    """Price of the model calls a run would make: prompt ≈ characters ÷ 4, 512 out each."""
    spec = llm.spec(GRADING_ROLE)
    total = 0.0
    for c in found.candidates:
        if not _paid(c):
            continue
        chars = len(c.item["stem"]) + len(str(c.response.get("text", ""))) + 2000
        cost = price_usd(
            spec.provider, spec.model, Usage(input_tokens=chars // 4, output_tokens=512)
        )
        if cost is None:
            return None
        total += cost
    return total


async def _regrade_one(
    llm: LLMClient, c: Candidate, prompt: Literal["current", "recorded"]
) -> GradeResult:
    item = c.item
    if c.grading["grader"] == "auto":
        return auto_grade(ItemType(item["item_type"]), item["answer_key"] or {}, c.response)
    by_kc = {entry["kc_id"]: entry["criteria"] for entry in (c.rubric or {}).get("components", [])}
    components = [
        GradedComponent(
            kc_id=uuid.UUID(comp["kc_id"]),
            name=comp["name"],
            description=comp.get("description") or "",
            criteria=by_kc.get(comp["kc_id"]),
        )
        for comp in item["components"]
    ]
    # Transient, never added to a session: the grader reads only `.criteria`.
    rubric = Rubric(criteria=c.rubric["criteria"]) if c.rubric else None
    system = c.prompt["system"] if prompt == "recorded" and c.prompt else None
    result, _usage = await grade_open(
        llm,
        stem=item["stem"],
        response=c.response,
        rubric=rubric,
        components=components,
        system=system,
    )
    return result


async def run(
    llm: LLMClient, found: Plan, *, prompt: Literal["current", "recorded"] = "current"
) -> Report:
    compared: list[tuple[Candidate, GradeResult]] = []
    failed = 0
    # Background work: it yields at the background share of the deployment ceiling (S47)
    # rather than spending the last of it out from under learners' own turns.
    with attributed(feature="regrade", learner_id=None, conversation_id=None, background=True):
        for c in found.candidates:
            try:
                compared.append((c, await _regrade_one(llm, c, prompt)))
            except Exception:  # a refusal or provider failure is one failed comparison
                failed += 1
    if not compared:
        return Report(0, failed, None, None, None, [], {})
    overall = _agreement(compared)
    component_diffs = [
        abs(score - r.component_scores[uuid.UUID(kc)])
        for c, r in compared
        for kc, score in c.component_scores.items()
        if uuid.UUID(kc) in r.component_scores
    ]
    largest = sorted(compared, key=lambda pair: abs(pair[0].score - pair[1].score), reverse=True)
    graders = sorted({c.grading["grader"] for c, _r in compared})
    return Report(
        compared=overall.compared,
        failed=failed,
        correct_agreement=overall.correct_agreement,
        mean_abs_score_diff=overall.mean_abs_score_diff,
        component_mean_abs_diff=(
            sum(component_diffs) / len(component_diffs) if component_diffs else None
        ),
        largest=[Comparison(c.event_id, c.score, r.score) for c, r in largest[:LARGEST]],
        by_grader={
            g: _agreement([(c, r) for c, r in compared if c.grading["grader"] == g])
            for g in graders
        },
    )


def _agreement(pairs: list[tuple[Candidate, GradeResult]]) -> Agreement:
    return Agreement(
        compared=len(pairs),
        correct_agreement=sum(c.correct == r.correct for c, r in pairs) / len(pairs),
        mean_abs_score_diff=sum(abs(c.score - r.score) for c, r in pairs) / len(pairs),
    )


def as_json(report: Report) -> dict:
    return {
        "compared": report.compared,
        "failed": report.failed,
        "correct_agreement": report.correct_agreement,
        "mean_abs_score_diff": report.mean_abs_score_diff,
        "component_mean_abs_diff": report.component_mean_abs_diff,
        "by_grader": {
            g: {
                "compared": a.compared,
                "correct_agreement": a.correct_agreement,
                "mean_abs_score_diff": a.mean_abs_score_diff,
            }
            for g, a in report.by_grader.items()
        },
        "largest": [
            {"event_id": str(c.event_id), "recorded": c.recorded, "regraded": c.regraded}
            for c in report.largest
        ],
    }


def render(found: Plan, report: Report | None, cost: float | None) -> str:
    graders = ", ".join(f"{k} {v}" for k, v in sorted(found.by_grader.items())) or "none"
    lines = [
        f"re-gradable attempts: {len(found.candidates)} ({graders})",
        f"not re-gradable: {found.not_regradable}",
        "estimated cost: " + ("unknown (unpriced model)" if cost is None else f"${cost:.4f}"),
    ]
    if report is None:
        lines.append("dry run — pass --run to grade")
        return "\n".join(lines)
    lines += [
        f"compared: {report.compared}, failed: {report.failed}",
        f"agreement on correct: {_pct(report.correct_agreement)}",
        f"mean |score difference|: {_num(report.mean_abs_score_diff)}",
        f"per-component mean |difference|: {_num(report.component_mean_abs_diff)}",
    ]
    lines += [
        f"  {g}: {a.compared} compared, agreement {_pct(a.correct_agreement)}, "
        f"mean |difference| {_num(a.mean_abs_score_diff)}"
        for g, a in report.by_grader.items()
    ]
    lines.append("largest disagreements:")
    lines += [
        f"  {c.event_id}: recorded {c.recorded:.2f} → {c.regraded:.2f}" for c in report.largest
    ]
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"
