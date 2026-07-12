"""The learner-profile dimension catalog: pure/LLM estimators over a learner's history.

Same shape as ``kc_tagging.py``/``placement_inference.py``: numbered-list prompts for the
LLM-backed estimators, tolerant parsing, best-effort (an estimator that can't produce a
confident value returns ``None`` rather than fabricating one). ``DIMENSION_SPECS`` is the
single source of truth for the dimension catalog — adding or dropping a dimension is a change
to this list only, never a migration (see ``app/models/profile.py``).
"""

import json
import statistics
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.models.assessment import Item, ItemType
from app.models.chat import Conversation, Message
from app.models.learning import LearningEvent

PROFILE_LLM_ROLE = ModelRole.FAST
"""Every LLM-backed profile estimator is a cheap classification task — the FAST tier."""

Kind = Literal["trait", "state"]
Source = Literal["behavioral", "self_report"]


@dataclass(frozen=True)
class DimensionEstimate:
    """One estimator's output: a JSON-serializable value plus its uncertainty."""

    value: Any
    uncertainty: float


@dataclass(frozen=True)
class EstimatorContext:
    """Everything an estimator might need, pre-loaded once and shared across the catalog.

    ``events``/``messages`` cover the learner's full history — trait estimators read them
    as-is, state estimators (e.g. ``engagement``) window down to the most recent session
    themselves. ``session``/``llm`` are here for the minority of estimators that need an
    extra DB lookup (``format_effectiveness`` joins to ``Item``) or a model call.
    """

    session: AsyncSession
    learner_id: uuid.UUID
    events: Sequence[LearningEvent]
    messages: Sequence[Message]
    llm: LLMClient


EstimatorFn = Callable[[EstimatorContext], Awaitable[tuple[DimensionEstimate | None, Usage]]]
"""Every estimator returns its ``Usage`` alongside the estimate (zero for non-LLM estimators)
so the orchestrator can log LLM cost uniformly — same shape as ``kc_tagging.tag_chunk`` etc."""


@dataclass(frozen=True)
class DimensionSpec:
    """One catalog entry: a dimension key plus the estimator that computes it."""

    key: str
    kind: Kind
    source: Source
    estimate: EstimatorFn


def _uncertainty(n: int, *, floor: float = 0.15) -> float:
    """Shrink uncertainty as evidence accumulates. Same v1-heuristic spirit as placement's
    ability/uncertainty mapping — not calibrated, revisit once real data exists."""
    return max(floor, 1.0 / (1.0 + 0.3 * n))


def _cluster_sessions(
    events: Sequence[LearningEvent], *, gap_minutes: int = 30
) -> list[list[LearningEvent]]:
    """Split a chronological event stream into sessions wherever the gap exceeds ``gap_minutes``.

    A simple, well-established heuristic (no session concept exists in the schema); shared by
    every estimator that needs session-scoped or session-aggregated behavior.
    """
    ordered = sorted(events, key=lambda e: e.created_at)
    if not ordered:
        return []
    sessions: list[list[LearningEvent]] = [[ordered[0]]]
    for prev, curr in pairwise(ordered):
        gap = (curr.created_at - prev.created_at).total_seconds() / 60.0
        if gap > gap_minutes:
            sessions.append([])
        sessions[-1].append(curr)
    return sessions


def _observations(events: Sequence[LearningEvent]) -> list[LearningEvent]:
    """Graded interactions only — excludes ``placement_seed`` rows, which carry a different
    payload shape (ability/uncertainty, not score/difficulty/latency)."""
    return [e for e in events if e.event_type == "observation"]


# --- Cognitive & pace -----------------------------------------------------

PACE_MIN_EVENTS = 3
OPTIMAL_CHALLENGE_MIN_EVENTS = 3
COGNITIVE_LOAD_MIN_SESSION_EVENTS = 4
ERROR_TYPE_MIN_INCORRECT = 3
ERROR_TYPE_MAX_SAMPLE = 10

_ERROR_TYPES = ("conceptual", "procedural", "careless")

_ERROR_TYPE_SYSTEM_PROMPT = (
    "A learner answered several questions incorrectly. You are given a numbered list of "
    "attempts (the question, what they answered, and the correct answer or grading "
    'criteria). Classify each as the most likely cause: "conceptual" (misunderstood the '
    'underlying idea), "procedural" (understood the idea but made a process/method error), '
    'or "careless" (right idea and method, wrong execution — looks like a slip). Respond '
    'with ONLY a JSON object {"classifications": [{"item": <number>, "type": "conceptual"|'
    '"procedural"|"careless"}]} and nothing else.'
)


async def _estimate_pace(ctx: EstimatorContext) -> tuple[DimensionEstimate | None, Usage]:
    seconds = [
        e.payload["latency_ms"] / 1000.0
        for e in _observations(ctx.events)
        if isinstance(e.payload.get("latency_ms"), int | float)
    ]
    if len(seconds) < PACE_MIN_EVENTS:
        return None, Usage()
    mid = len(seconds) // 2
    first_med = statistics.median(seconds[:mid])
    second_med = statistics.median(seconds[mid:])
    if second_med < first_med * 0.9:
        trend = "speeding_up"
    elif second_med > first_med * 1.1:
        trend = "slowing_down"
    else:
        trend = "stable"
    value = {"median_seconds": round(statistics.median(seconds), 1), "trend": trend}
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(seconds))), Usage()


async def _estimate_optimal_challenge(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    graded = _observations(ctx.events)
    if len(graded) < OPTIMAL_CHALLENGE_MIN_EVENTS:
        return None, Usage()
    band = [e for e in graded if 0.4 <= e.payload.get("score", 0.0) <= 0.8]
    if len(band) >= OPTIMAL_CHALLENGE_MIN_EVENTS:
        difficulty = statistics.mean(e.payload.get("difficulty", 0.0) for e in band)
        uncertainty = _uncertainty(len(band))
    else:
        difficulty = statistics.mean(e.payload.get("difficulty", 0.0) for e in graded)
        uncertainty = min(1.0, _uncertainty(len(graded)) + 0.2)  # fallback: wider uncertainty
    return DimensionEstimate(value=round(difficulty, 2), uncertainty=uncertainty), Usage()


async def _estimate_cognitive_load_tolerance(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    sessions = _cluster_sessions(
        _observations(ctx.events), gap_minutes=get_settings().profile_session_gap_minutes
    )
    deltas = []
    for session_events in sessions:
        if len(session_events) < COGNITIVE_LOAD_MIN_SESSION_EVENTS:
            continue
        mid = len(session_events) // 2
        first_acc = statistics.mean(e.payload.get("score", 0.0) for e in session_events[:mid])
        second_acc = statistics.mean(e.payload.get("score", 0.0) for e in session_events[mid:])
        deltas.append(second_acc - first_acc)
    if not deltas:
        return None, Usage()
    value = round(statistics.mean(deltas), 3)
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(deltas))), Usage()


def _describe_answer(item: Item, response: dict | None) -> str:
    response = response or {}
    item_type = ItemType(item.item_type)
    answer_key = item.answer_key or {}
    if item_type is ItemType.MCQ and answer_key:
        choices = answer_key.get("choices", [])
        correct_idx, chosen_idx = answer_key.get("correct"), response.get("choice")
        correct_text = (
            choices[correct_idx]
            if isinstance(correct_idx, int) and correct_idx < len(choices)
            else "?"
        )
        chosen_text = (
            choices[chosen_idx]
            if isinstance(chosen_idx, int) and 0 <= chosen_idx < len(choices)
            else "?"
        )
        return f"answered {chosen_text!r}, correct answer was {correct_text!r}"
    if item_type in (ItemType.CLOZE, ItemType.FILL_BLANK) and answer_key:
        return (
            f"answered {response.get('blanks')!r}, correct blanks were {answer_key.get('blanks')!r}"
        )
    if item.rubric is not None and item.rubric.criteria:
        return f"answered {response.get('text', '')!r}; graded against criteria {item.rubric.criteria!r}"
    return f"answered {response!r} (no fixed answer key)"


def _build_error_type_prompt(candidates: Sequence[tuple[LearningEvent, Item]]) -> str:
    numbered = "\n".join(
        f"{i}. Question: {item.stem}\n   {_describe_answer(item, e.payload.get('response'))}"
        for i, (e, item) in enumerate(candidates, start=1)
    )
    return f"Incorrect attempts:\n{numbered}"


def _parse_error_types(content: str, n_candidates: int) -> dict[str, int]:
    counts = dict.fromkeys(_ERROR_TYPES, 0)
    try:
        raw = json.loads(_extract_json(content))["classifications"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return counts
    if not isinstance(raw, list):
        return counts
    seen: set[int] = set()
    for entry in raw:
        try:
            index = int(entry["item"])
            label = str(entry["type"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= index <= n_candidates or label not in _ERROR_TYPES or index in seen:
            continue
        seen.add(index)
        counts[label] += 1
    return counts


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]


async def _estimate_error_type(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    incorrect = [
        e
        for e in _observations(ctx.events)
        if e.payload.get("score", 1.0) < 0.5 and e.payload.get("item_id")
    ]
    if len(incorrect) < ERROR_TYPE_MIN_INCORRECT:
        return None, Usage()
    sample = incorrect[-ERROR_TYPE_MAX_SAMPLE:]
    item_ids = {uuid.UUID(e.payload["item_id"]) for e in sample}
    items = {
        item.id: item
        for item in (
            await ctx.session.scalars(
                select(Item).where(Item.id.in_(item_ids)).options(selectinload(Item.rubric))
            )
        ).all()
    }
    candidates = [
        (e, items[uuid.UUID(e.payload["item_id"])])
        for e in sample
        if uuid.UUID(e.payload["item_id"]) in items
    ]
    if len(candidates) < ERROR_TYPE_MIN_INCORRECT:
        return None, Usage()
    completion = await ctx.llm.complete(
        PROFILE_LLM_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_error_type_prompt(candidates))],
        system=_ERROR_TYPE_SYSTEM_PROMPT,
        max_tokens=512,
    )
    counts = _parse_error_types(completion.content, len(candidates))
    total = sum(counts.values())
    if total == 0:
        return None, completion.usage
    distribution = {k: round(v / total, 2) for k, v in counts.items()}
    return DimensionEstimate(value=distribution, uncertainty=_uncertainty(total)), completion.usage


# --- Metacognition & self-regulation ---------------------------------------

HELP_SEEKING_MIN_EVENTS = 3
PERSISTENCE_MIN_ITEMS = 2


async def _estimate_help_seeking(ctx: EstimatorContext) -> tuple[DimensionEstimate | None, Usage]:
    hints = [
        e.payload["hints_used"]
        for e in _observations(ctx.events)
        if isinstance(e.payload.get("hints_used"), int)
    ]
    if len(hints) < HELP_SEEKING_MIN_EVENTS:
        return None, Usage()
    value = round(statistics.mean(hints), 2)
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(hints))), Usage()


async def _estimate_persistence(ctx: EstimatorContext) -> tuple[DimensionEstimate | None, Usage]:
    # A single graded interaction fans out into one LearningEvent per tagged KC on a
    # multi-KC item (mastery.record_observation) — all sharing the same created_at. Collapse
    # those back into one attempt per (item_id, created_at) before counting attempts, or a
    # single multi-KC answer would masquerade as multiple retries below.
    by_item: dict[str, dict[datetime, LearningEvent]] = {}
    for e in _observations(ctx.events):
        item_id = e.payload.get("item_id")
        if item_id:
            by_item.setdefault(item_id, {}).setdefault(e.created_at, e)
    qualifying = [
        attempts
        for by_time in by_item.values()
        for attempts in [list(by_time.values())]
        if len(attempts) >= 2 and attempts[0].payload.get("score", 1.0) < 0.5
    ]
    if len(qualifying) < PERSISTENCE_MIN_ITEMS:
        return None, Usage()
    bounced = sum(
        1
        for attempts in qualifying
        if any(a.payload.get("score", 0.0) >= 0.5 for a in attempts[1:])
    )
    value = round(bounced / len(qualifying), 2)
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(qualifying))), Usage()


# --- Motivation & affect ----------------------------------------------------

ENGAGEMENT_MIN_SESSION_EVENTS = 3
GOAL_ORIENTATIONS = ("mastery", "performance")

_GOAL_ORIENTATION_SYSTEM_PROMPT = (
    "A learner stated one or more goals for what they want to learn. Classify their "
    'predominant orientation: "mastery" (focused on understanding and skill for its own '
    'sake) or "performance" (focused on grades, scores, or proving ability to others). '
    'Respond with ONLY a JSON object {"orientation": "mastery"|"performance", "confidence": '
    "<0.0-1.0>} and nothing else."
)


async def _estimate_engagement(ctx: EstimatorContext) -> tuple[DimensionEstimate | None, Usage]:
    sessions = _cluster_sessions(
        _observations(ctx.events), gap_minutes=get_settings().profile_session_gap_minutes
    )
    if not sessions:
        return None, Usage()
    latest = sessions[-1]
    if len(latest) < ENGAGEMENT_MIN_SESSION_EVENTS:
        return None, Usage()
    longest_streak = current = 0
    for e in latest:
        if e.payload.get("score", 1.0) < 0.5:
            current += 1
            longest_streak = max(longest_streak, current)
        else:
            current = 0
    value = round(1.0 - min(1.0, longest_streak / len(latest)), 2)
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(latest))), Usage()


def _parse_goal_orientation(content: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_extract_json(content))
        orientation = str(data["orientation"])
        confidence = float(data.get("confidence", 0.5))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if orientation not in GOAL_ORIENTATIONS:
        return None
    return {"orientation": orientation, "confidence": max(0.0, min(1.0, confidence))}


async def _estimate_goal_orientation(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    goals = [
        g
        for g in (
            await ctx.session.scalars(
                select(Conversation.goal)
                .where(Conversation.learner_id == ctx.learner_id, Conversation.goal.is_not(None))
                .order_by(Conversation.created_at.desc())
                .limit(5)
            )
        ).all()
        if g
    ]
    if not goals:
        return None, Usage()
    prompt = "Learner's stated goals:\n" + "\n".join(f"- {g}" for g in goals)
    completion = await ctx.llm.complete(
        PROFILE_LLM_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=_GOAL_ORIENTATION_SYSTEM_PROMPT,
        max_tokens=128,
    )
    parsed = _parse_goal_orientation(completion.content)
    if parsed is None:
        return None, completion.usage
    uncertainty = max(0.15, 1.0 - parsed["confidence"])
    return DimensionEstimate(value=parsed, uncertainty=uncertainty), completion.usage


# --- Context & preferences --------------------------------------------------

INTERESTS_MAX_MESSAGES = 20
INTERESTS_MAX_CHARS_PER_MESSAGE = 500
INTERESTS_MAX_TAGS = 5
READING_LEVEL_MIN_WORDS = 30
SESSION_LOGISTICS_MIN_SESSIONS = 2
FORMAT_EFFECTIVENESS_MIN_TYPES = 2
FORMAT_EFFECTIVENESS_MIN_PER_TYPE = 3

_INTERESTS_SYSTEM_PROMPT = (
    "A learner's own chat messages are shown below. Extract up to 5 short topics or "
    "interests that could be used to pick relatable examples and analogies when teaching "
    'them (e.g. "basketball", "cooking", "video games"). Only include things the messages '
    "actually give evidence for — do not guess. Respond with ONLY a JSON object "
    '{"interests": ["<topic>", ...]} and nothing else. If nothing applies, return an empty list.'
)


def _parse_interests(content: str) -> list[str]:
    try:
        raw = json.loads(_extract_json(content))["interests"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= INTERESTS_MAX_TAGS:
            break
    return tags


async def _estimate_interests(ctx: EstimatorContext) -> tuple[DimensionEstimate | None, Usage]:
    own_messages = [m.content for m in ctx.messages if m.content.strip()]
    if not own_messages:
        return None, Usage()
    sample = own_messages[-INTERESTS_MAX_MESSAGES:]
    text = "\n".join(m[:INTERESTS_MAX_CHARS_PER_MESSAGE] for m in sample)
    completion = await ctx.llm.complete(
        PROFILE_LLM_ROLE,
        [ChatMessage(role=ChatRole.USER, content=f"Learner's messages:\n{text}")],
        system=_INTERESTS_SYSTEM_PROMPT,
        max_tokens=128,
    )
    interests = _parse_interests(completion.content)
    if not interests:
        return None, completion.usage
    return (
        DimensionEstimate(value=interests, uncertainty=_uncertainty(len(sample))),
        completion.usage,
    )


def _syllable_count(word: str) -> int:
    word = word.lower().strip(".,!?;:\"'()")
    if not word:
        return 0
    vowels = "aeiouy"
    count = 0
    prev_was_vowel = False
    for ch in word:
        is_vowel = ch in vowels
        if is_vowel and not prev_was_vowel:
            count += 1
        prev_was_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


async def _estimate_reading_level(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    text = " ".join(m.content for m in ctx.messages if m.content.strip())
    words = text.split()
    if len(words) < READING_LEVEL_MIN_WORDS:
        return None, Usage()
    sentences = max(1, sum(text.count(c) for c in ".!?"))
    syllables = sum(_syllable_count(w) for w in words)
    grade = 0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59
    value = max(0.0, round(grade, 1))
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(words) // 10)), Usage()


async def _estimate_session_logistics(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    sessions = _cluster_sessions(
        _observations(ctx.events), gap_minutes=get_settings().profile_session_gap_minutes
    )
    if len(sessions) < SESSION_LOGISTICS_MIN_SESSIONS:
        return None, Usage()
    durations, hours = [], []
    for session_events in sessions:
        start, end = session_events[0].created_at, session_events[-1].created_at
        durations.append(max(1.0, (end - start).total_seconds() / 60.0))
        hours.append(start.hour)
    value = {
        "typical_session_minutes": round(statistics.median(durations), 1),
        "preferred_hour_utc": statistics.mode(hours),
    }
    return DimensionEstimate(value=value, uncertainty=_uncertainty(len(sessions))), Usage()


async def _estimate_format_effectiveness(
    ctx: EstimatorContext,
) -> tuple[DimensionEstimate | None, Usage]:
    graded = [e for e in _observations(ctx.events) if e.payload.get("item_id")]
    if not graded:
        return None, Usage()
    item_ids = {uuid.UUID(e.payload["item_id"]) for e in graded}
    items = {
        item.id: item
        for item in (await ctx.session.scalars(select(Item).where(Item.id.in_(item_ids)))).all()
    }
    by_type: dict[str, list[LearningEvent]] = {}
    for e in graded:
        item = items.get(uuid.UUID(e.payload["item_id"]))
        if item is not None:
            by_type.setdefault(item.item_type, []).append(e)
    qualifying = {
        t: evs for t, evs in by_type.items() if len(evs) >= FORMAT_EFFECTIVENESS_MIN_PER_TYPE
    }
    if len(qualifying) < FORMAT_EFFECTIVENESS_MIN_TYPES:
        return None, Usage()
    value = {
        t: {
            "mean_score": round(statistics.mean(e.payload.get("score", 0.0) for e in evs), 2),
            "mean_difficulty": round(
                statistics.mean(e.payload.get("difficulty", 0.0) for e in evs), 2
            ),
            "n": len(evs),
        }
        for t, evs in qualifying.items()
    }
    total = sum(len(evs) for evs in qualifying.values())
    return DimensionEstimate(value=value, uncertainty=_uncertainty(total)), Usage()


DIMENSION_SPECS: list[DimensionSpec] = [
    DimensionSpec(key="pace", kind="trait", source="behavioral", estimate=_estimate_pace),
    DimensionSpec(
        key="optimal_challenge",
        kind="trait",
        source="behavioral",
        estimate=_estimate_optimal_challenge,
    ),
    DimensionSpec(
        key="error_type", kind="trait", source="behavioral", estimate=_estimate_error_type
    ),
    DimensionSpec(
        key="cognitive_load_tolerance",
        kind="trait",
        source="behavioral",
        estimate=_estimate_cognitive_load_tolerance,
    ),
    DimensionSpec(
        key="help_seeking", kind="trait", source="behavioral", estimate=_estimate_help_seeking
    ),
    DimensionSpec(
        key="persistence", kind="trait", source="behavioral", estimate=_estimate_persistence
    ),
    DimensionSpec(
        key="engagement", kind="state", source="behavioral", estimate=_estimate_engagement
    ),
    DimensionSpec(
        key="goal_orientation",
        kind="trait",
        source="self_report",
        estimate=_estimate_goal_orientation,
    ),
    DimensionSpec(key="interests", kind="trait", source="behavioral", estimate=_estimate_interests),
    DimensionSpec(
        key="reading_level", kind="trait", source="behavioral", estimate=_estimate_reading_level
    ),
    DimensionSpec(
        key="session_logistics",
        kind="trait",
        source="behavioral",
        estimate=_estimate_session_logistics,
    ),
    DimensionSpec(
        key="format_effectiveness",
        kind="trait",
        source="behavioral",
        estimate=_estimate_format_effectiveness,
    ),
]
"""The dimension catalog. Populated incrementally (see the learner-profile plan's commit
sequence) — each family's estimators are added here as they're implemented."""
