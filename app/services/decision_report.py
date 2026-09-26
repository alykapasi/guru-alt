"""What the shadow decisions would have done (S82) — ``uv run poe decision-report``.

A question goes live when a person has read this and decided to switch it (spec §8), so the
report leads with the numbers that decide that, per question:

- ``intent``: agreement with the FAST gate, and the two ways a confident disagreement would
  hurt — grading a non-answer, or dropping a real attempt.
- ``fully_correct``: **false passes** — answers Jev was confident were fully correct that the
  SMART grader failed. A false pass writes wrong mastery evidence, so it is the number that
  decides whether this question may ever skip the grader.

Rows without a baseline (a live answer that was used) cannot be compared and are left out of
agreement; they still count for spend, latency and status.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.rubric_grading import PASS_THRESHOLD
from app.learning.turn_read import FULLY_CORRECT, INTENT
from app.models.chat import LLMCall, Message
from app.models.decision import DecisionCall
from app.models.learning import LearningEvent

_BANDS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.0))
_EXAMPLE_CHARS = 200


@dataclass(frozen=True)
class Band:
    low: float
    high: float
    rows: int
    agreed: int


@dataclass(frozen=True)
class IntentSummary:
    compared: int
    agreed: int
    bands: tuple[Band, ...]
    confusion: dict[tuple[str, str], int]  # (jev, baseline) -> count
    confident: int
    confident_agreed: int
    graded_non_answers: int  # confident "attempt" where the gate said otherwise
    dropped_attempts: int  # confident "withdrawal" where the gate said "attempt"


@dataclass(frozen=True)
class GradeSummary:
    compared: int
    confident: int
    below_full: int
    mean_shortfall: float | None
    false_passes: int


@dataclass(frozen=True)
class Operations:
    requests: int
    statuses: dict[str, int]
    p50: int | None
    p95: int | None
    p99: int | None
    spend_usd: float


def _band_of(value: float) -> int:
    for i, (low, high) in enumerate(_BANDS):
        if low <= value < high:
            return i
    return len(_BANDS) - 1  # exactly 1.0


def _compared_intents(rows: Sequence[DecisionCall]) -> list[DecisionCall]:
    return [
        r
        for r in rows
        if r.question == INTENT
        and r.status == "ok"
        and r.answer is not None
        and r.confidence is not None
        and r.baseline_intent is not None
    ]


def summarise_intent(rows: Sequence[DecisionCall], *, threshold: float) -> IntentSummary:
    compared = _compared_intents(rows)
    counts = [[0, 0] for _ in _BANDS]
    confusion: Counter[tuple[str, str]] = Counter()
    for r in compared:
        band = counts[_band_of(r.confidence or 0.0)]
        band[0] += 1
        band[1] += r.answer == r.baseline_intent
        confusion[(r.answer or "", r.baseline_intent or "")] += 1
    confident = [r for r in compared if (r.confidence or 0.0) >= threshold]
    return IntentSummary(
        compared=len(compared),
        agreed=sum(r.answer == r.baseline_intent for r in compared),
        bands=tuple(
            Band(low=low, high=high, rows=n, agreed=a)
            for (low, high), (n, a) in zip(_BANDS, counts, strict=True)
        ),
        confusion=dict(confusion),
        confident=len(confident),
        confident_agreed=sum(r.answer == r.baseline_intent for r in confident),
        graded_non_answers=sum(
            r.answer == "attempt" and r.baseline_intent != "attempt" for r in confident
        ),
        dropped_attempts=sum(
            r.answer == "withdrawal" and r.baseline_intent == "attempt" for r in confident
        ),
    )


def _p_yes(r: DecisionCall) -> float | None:
    return (r.probabilities or {}).get("yes")


def _compared_grades(rows: Sequence[DecisionCall]) -> list[DecisionCall]:
    return [
        r
        for r in rows
        if r.question == FULLY_CORRECT
        and r.status == "ok"
        and _p_yes(r) is not None
        and r.baseline_score is not None
    ]


def _confident_grades(rows: Sequence[DecisionCall], threshold: float) -> list[DecisionCall]:
    return [r for r in _compared_grades(rows) if (_p_yes(r) or 0.0) >= threshold]


def summarise_grade(rows: Sequence[DecisionCall], *, threshold: float) -> GradeSummary:
    compared = _compared_grades(rows)
    confident = _confident_grades(rows, threshold)
    shortfalls = [
        1.0 - (r.baseline_score or 0.0) for r in confident if (r.baseline_score or 0.0) < 1.0
    ]
    return GradeSummary(
        compared=len(compared),
        confident=len(confident),
        below_full=len(shortfalls),
        mean_shortfall=sum(shortfalls) / len(shortfalls) if shortfalls else None,
        false_passes=sum((r.baseline_score or 0.0) < PASS_THRESHOLD for r in confident),
    )


def _percentile(values: Sequence[int], q: float) -> int | None:
    """Nearest-rank percentile of already-sorted ``values``."""
    if not values:
        return None
    return values[max(1, math.ceil(len(values) * q)) - 1]


def summarise_operations(rows: Sequence[DecisionCall]) -> Operations:
    first_row_of_request: dict[uuid.UUID, DecisionCall] = {}
    for r in rows:
        first_row_of_request.setdefault(r.request_id, r)
    requests = list(first_row_of_request.values())
    latencies = sorted(r.latency_ms for r in requests if r.latency_ms is not None)
    return Operations(
        requests=len(requests),
        statuses=dict(Counter(r.status for r in rows)),
        p50=_percentile(latencies, 0.50),
        p95=_percentile(latencies, 0.95),
        p99=_percentile(latencies, 0.99),
        spend_usd=sum((r.cost_usd or 0.0 for r in requests), 0.0),
    )


async def _mean_cost(session: AsyncSession, role: str, since: datetime | None) -> float | None:
    query = select(func.avg(LLMCall.cost_usd)).where(
        LLMCall.role == role, LLMCall.cost_usd.is_not(None)
    )
    if since is not None:
        query = query.where(LLMCall.created_at >= since.replace(tzinfo=None))
    value = await session.scalar(query)
    return float(value) if value is not None else None


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _EXAMPLE_CHARS else text[: _EXAMPLE_CHARS - 1] + "…"


async def _reply_to(session: AsyncSession, row: DecisionCall) -> str:
    """The learner's words behind a row, found through ids — the row itself stores none."""
    if row.attempt_id is not None:
        event = await session.scalar(
            select(LearningEvent).where(LearningEvent.attempt_id == row.attempt_id).limit(1)
        )
        if event is not None:
            return _clip(str((event.payload.get("response") or {}).get("text", "")))
    if row.conversation_id is not None:
        # The gate runs around the time the learner's message is written; take the closest.
        message = await session.scalar(
            select(Message)
            .where(Message.conversation_id == row.conversation_id, Message.role == "user")
            .order_by(func.abs(func.extract("epoch", Message.created_at - row.created_at)))
            .limit(1)
        )
        if message is not None:
            return _clip(message.content)
    return "(not found)"


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


async def build_report(
    session: AsyncSession,
    *,
    since: datetime | None,
    intent_threshold: float,
    grade_threshold: float,
    examples: int,
) -> str:
    query = select(DecisionCall).order_by(DecisionCall.created_at)
    if since is not None:
        query = query.where(DecisionCall.created_at >= since.replace(tzinfo=None))
    rows = list((await session.scalars(query)).all())

    intent = summarise_intent(rows, threshold=intent_threshold)
    grade = summarise_grade(rows, threshold=grade_threshold)
    ops = summarise_operations(rows)
    fast_cost = await _mean_cost(session, "fast", since)
    smart_cost = await _mean_cost(session, "smart", since)

    lines = [
        f"Decision report — {len(rows)} row(s) over {ops.requests} request(s)",
        "",
        f"intent (threshold {intent_threshold})",
        f"  agreement with the FAST gate: {intent.agreed}/{intent.compared}",
    ]
    for band in intent.bands:
        lines.append(
            f"    confidence {band.low:.1f}-{band.high:.1f}: {band.agreed}/{band.rows} agree"
        )
    lines += [
        f"  confident enough to decide: {intent.confident}/{intent.compared}"
        f" ({intent.confident_agreed} of them agree)",
        f"  would have graded a non-answer: {intent.graded_non_answers}",
        f"  would have dropped a real attempt: {intent.dropped_attempts}",
        f"  FAST calls that would have been skipped: {intent.confident}"
        f" (≈ {_money(fast_cost * intent.confident if fast_cost is not None else None)}"
        " at the mean FAST call)",
        "  confusion (jev → gate): "
        + (", ".join(f"{j}→{b}: {n}" for (j, b), n in sorted(intent.confusion.items())) or "none"),
        "",
        f"fully_correct (threshold {grade_threshold})",
        f"  compared with the SMART grade: {grade.compared}",
        f"  confident passes: {grade.confident}",
        f"  FALSE PASSES (SMART scored below {PASS_THRESHOLD}): {grade.false_passes}",
        f"  confident passes SMART scored below 1.0: {grade.below_full}"
        + (f", mean shortfall {grade.mean_shortfall:.2f}" if grade.mean_shortfall else ""),
        f"  SMART calls that would have been skipped: {grade.confident}"
        f" (≈ {_money(smart_cost * grade.confident if smart_cost is not None else None)}"
        " at the mean SMART call — grading calls are not told apart from tutor turns in"
        " llm_calls, so this is a rough figure)",
        "",
        "operations",
        "  status: " + ", ".join(f"{k} {v}" for k, v in sorted(ops.statuses.items())),
        f"  latency ms p50 {ops.p50} · p95 {ops.p95} · p99 {ops.p99}",
        f"  Jev spend: ${ops.spend_usd:.6f}",
    ]

    if examples > 0:
        false_passes = [
            r
            for r in _confident_grades(rows, grade_threshold)
            if (r.baseline_score or 0.0) < PASS_THRESHOLD
        ][:examples]
        disagreements = [
            r
            for r in _compared_intents(rows)
            if (r.confidence or 0.0) >= intent_threshold and r.answer != r.baseline_intent
        ][:examples]
        if false_passes:
            lines += ["", "false passes"]
            for r in false_passes:
                lines.append(
                    f"  P(yes) {_p_yes(r):.2f} vs SMART {r.baseline_score:.2f}:"
                    f" {await _reply_to(session, r)}"
                )
        if disagreements:
            lines += ["", "confident intent disagreements"]
            for r in disagreements:
                lines.append(
                    f"  jev {r.answer} ({r.confidence:.2f}) vs gate {r.baseline_intent}:"
                    f" {await _reply_to(session, r)}"
                )
    return "\n".join(lines)
