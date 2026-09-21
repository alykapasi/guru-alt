# Evidence Kinds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a flashcard self-rating from moving a learner's measured ability, and give self-rating a working interaction so the fix is load-bearing rather than theoretical.

**Architecture:** The grader — the only thing that knows whether a judgement was made or reported — stamps an `EvidenceKind` on its result, which rides the `Observation` into the tracer. The tracer branches once: self-reported evidence advances FSRS scheduling and writes a `self_report` event, but touches neither `ability`, `uncertainty`, nor `last_seen_at`. Because every existing reader of the event log filters `event_type == "observation"` explicitly, self-reports are excluded by default; the handful of sites that should keep seeing them opt back in by name.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, pytest; React + TypeScript + Vite + vitest on the frontend.

**Spec:** [docs/superpowers/specs/2026-09-21-s56-evidence-kinds-design.md](../specs/2026-09-21-s56-evidence-kinds-design.md)

## Global Constraints

- Python 3.13; ruff line-length 100.
- `uv run poe check` (lint + type-check + test) green before every commit.
- `uv run poe format-check` green before every commit.
- `uv run poe api-contract` green on any commit touching `app/api/` or `app/schemas/`.
- Frontend type gate is `npm run build` from `frontend/`. **`npx tsc --noEmit` checks nothing in this repo — do not use it.**
- One tracker item id per commit subject: `[S56]` or `[S54]`.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Stage only the files the task names, then run `git status` and confirm nothing else is staged.
- **Never** `git reset`, `git commit --amend`, `git rebase`, or `git push --force`.
- **Do not open a pull request.** One PR covers all four slices of this workstream, later.
- Branch is `feat/evidence-kinds`, already created off `main`. Push after each task; pushing is a backup, not a review request.
- Every test added below must be **shown to fail before the fix and pass after**. A test that passes before the change proves nothing and must be rewritten.

---

## File Structure

**Created:**

- `db/migrations/versions/0057_self_report_events.py` — widens one partial index predicate.
- `tests/test_evidence_kinds.py` — the tracer split, the clock, and the per-consumer inclusion/exclusion matrix.
- `frontend/src/components/lessons/FlashcardPanel.tsx` — the reveal-then-rate interaction. Its own file because `ItemPanel` is a 51-line display component and folding a stateful three-phase interaction into it would double its size and its reasons to change.
- `frontend/src/components/lessons/FlashcardPanel.test.tsx` — reveal gating and copy.

**Modified:**

- `app/models/assessment.py` — `EvidenceKind` enum, beside `ItemType`/`SELF_GRADABLE`.
- `app/learning/grading.py` — `GradeResult.evidence_kind`; `grade_flashcard` stamps `SELF_REPORTED`.
- `app/learning/mastery.py` — `Observation.evidence_kind`; the branch in `record_observation`; `EVENT_SCHEMA_VERSION`; `recent_attempts_at_item`; `recent_struggle`; `KCEvidence`/`kc_evidence`.
- `app/models/learning.py` — the index predicate in `__table_args__`.
- `app/services/assessment.py` — `answer_item` copies the kind; `_recorded_grade` and `last_answered` accept both event types.
- `app/services/analytics.py` — `_NO_EVIDENCE`; streak/momentum accept both; `assessed` re-grounded.
- `app/schemas/analytics.py` — `KCMasteryRead.self_reported_attempts`.
- `app/services/notes.py` — the two activity probes accept both; the outcome sample does not.
- `app/schemas/chat.py` — `ChatTurnRequest.rating`.
- `app/api/v1/chat.py` — threads `rating` to the workflow turn.
- `app/api/v1/assessment.py` — the reveal endpoint.
- `app/services/workflow.py` — threads `rating` into the resume `Command`.
- `app/agent/state.py` — `WorkflowState.rating`.
- `app/agent/workflow.py` — `await_response` reads the rating; `grade` builds the response by item type.
- `app/services/session_runner.py` — `review_item_type` docstring.
- `frontend/src/api/sse.ts`, `frontend/src/api/hooks.ts` — reveal call and rating on the turn.
- `frontend/src/components/lessons/ItemPanel.tsx` — delegates flashcards to `FlashcardPanel`.

---

## Task 1: EvidenceKind on the grader and the observation

Pure plumbing: the field exists and is carried end to end, but nothing branches on it yet. Splitting it out means Task 2's diff is the behaviour change and nothing else.

**Files:**

- Modify: `app/models/assessment.py`
- Modify: `app/learning/grading.py:38-53` (`GradeResult`), `app/learning/grading.py:66-84` (`grade_flashcard`)
- Modify: `app/learning/mastery.py:47-115` (`Observation`)
- Modify: `app/services/assessment.py:383-400` (the `Observation(...)` construction)
- Test: `tests/test_evidence_kinds.py` (create), `tests/test_grading.py`

**Interfaces:**

- Produces: `app.models.assessment.EvidenceKind` — a `StrEnum` with members `DEMONSTRATED = "demonstrated"` and `SELF_REPORTED = "self_reported"`. `GradeResult.evidence_kind: EvidenceKind` and `Observation.evidence_kind: EvidenceKind`, both defaulting to `EvidenceKind.DEMONSTRATED`. Every later task depends on these exact names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_evidence_kinds.py`:

```python
"""Evidence kinds (S56): a self-rating is evidence about retention, not about ability."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import mastery
from app.learning.grading import auto_grade, grade_flashcard
from app.learning.mastery import Observation
from app.models.assessment import EvidenceKind, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent


async def _seed(
    session: AsyncSession, *, kc_slugs: Sequence[str] = ("a",)
) -> tuple[Learner, list[KC]]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [KC(topic_id=topic.id, slug=s, name=s) for s in kc_slugs]
    session.add_all(kcs)
    await session.flush()
    return learner, kcs


def test_a_self_rated_flashcard_is_marked_self_reported() -> None:
    assert grade_flashcard({"rating": 4}).evidence_kind is EvidenceKind.SELF_REPORTED


def test_a_deterministically_graded_answer_is_marked_demonstrated() -> None:
    result = auto_grade(ItemType.MCQ, {"correct": 1, "choices": ["a", "b"]}, {"choice": 1})
    assert result.evidence_kind is EvidenceKind.DEMONSTRATED


def test_an_observation_is_demonstrated_unless_it_says_otherwise() -> None:
    """The default is the safe one: a caller that forgets the field asserts nothing extra.

    Inverted, a forgotten field would silently downgrade real evidence to self-report and
    stop the tracer learning from it — a failure that looks like nothing at all.
    """
    obs = Observation(learner_id=uuid.uuid4(), kc_weights={uuid.uuid4(): 1.0}, score=1.0)
    assert obs.evidence_kind is EvidenceKind.DEMONSTRATED


def test_a_client_cannot_claim_its_answer_was_demonstrated() -> None:
    """The kind is derived, never accepted. Pydantic ignores unknown fields by default, so
    without this test a future `model_config = {"extra": "allow"}` would silently hand the
    browser control of whether its own rating counts as evidence.
    """
    from app.schemas.assessment import AnswerSubmit

    submission = AnswerSubmit.model_validate(
        {"response": {"rating": 4}, "evidence_kind": "demonstrated"}
    )
    assert not hasattr(submission, "evidence_kind")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_evidence_kinds.py -v`

Expected: FAIL — `ImportError: cannot import name 'EvidenceKind' from 'app.models.assessment'`.

- [ ] **Step 3: Add the enum**

In `app/models/assessment.py`, directly below the `SELF_GRADABLE` definition (line 37) and its docstring:

```python
class EvidenceKind(StrEnum):
    """Whether an attempt's score was *judged* or *reported* (S56).

    A rubric grade, an MCQ key and a cloze match are all judgements: something other than the
    learner decided how the answer went. A flashcard self-rating is the learner's own account
    of their recall. Both are real evidence and neither is noise — but they are evidence about
    different things, and the tracer must not fold them into the same number.

    Derived on the server from the grading path (see ``app.learning.grading``), never accepted
    from a request body. A bit the client can set is a bit the client can drop.
    """

    DEMONSTRATED = "demonstrated"
    SELF_REPORTED = "self_reported"
```

`StrEnum` is already imported in this file (it is what `ItemType` uses) — do not add a second import.

- [ ] **Step 4: Add the field to `GradeResult`**

In `app/learning/grading.py`, add to the `GradeResult` class body, after `diagnoses`:

```python
    evidence_kind: EvidenceKind = EvidenceKind.DEMONSTRATED
    """Judged, or reported by the learner (S56). Defaults to judged because every grading
    path here except ``grade_flashcard`` is one — and because the safe default for a caller
    that forgets is "this was real evidence", not "discard it"."""
```

Extend the existing import at the top of the file:

```python
from app.models.assessment import AUTO_GRADABLE, EvidenceKind, ItemType
```

- [ ] **Step 5: Stamp `grade_flashcard`**

In `app/learning/grading.py`, change the return of `grade_flashcard`:

```python
    return GradeResult(
        score=score,
        correct=score >= 0.75,
        detail={"rating": rating, "method": "self"},
        evidence_kind=EvidenceKind.SELF_REPORTED,
    )
```

Leave `detail["method"]` alone. It stays as human-readable detail; `evidence_kind` is now the field anything branches on.

- [ ] **Step 6: Add the field to `Observation`**

In `app/learning/mastery.py`, add to the `Observation` class body, after `kc_diagnoses` and its docstring:

```python
    evidence_kind: EvidenceKind = EvidenceKind.DEMONSTRATED
    """Whether this attempt was judged or self-reported (S56). Set from the grader's own
    ``GradeResult``, which is the only thing that knows. Self-reported evidence advances the
    review schedule and nothing else — see ``record_observation``."""
```

Add the import:

```python
from app.models.assessment import EvidenceKind
```

- [ ] **Step 7: Carry it through `answer_item`**

In `app/services/assessment.py`, inside the `Observation(...)` construction at line 383, add as the final keyword argument (after `detail=result.detail,`):

```python
        # From the grader, not the request: `AnswerSubmit` has no such field, so a client
        # cannot claim its self-rating was a demonstration.
        evidence_kind=result.evidence_kind,
    )
```

Extend the existing `from app.learning.grading import (...)` block only if `EvidenceKind` is needed by name here — it is not, because the value is read off `result`. Do not add an unused import.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_evidence_kinds.py tests/test_grading.py -v`

Expected: PASS, all.

- [ ] **Step 9: Run the full gate**

Run: `uv run poe check && uv run poe format-check`

Expected: all green. No behaviour changed, so no existing test should move.

- [ ] **Step 10: Commit**

```bash
git add app/models/assessment.py app/learning/grading.py app/learning/mastery.py \
        app/services/assessment.py tests/test_evidence_kinds.py
git status
git commit -F - <<'EOF'
feat(learning): record whether a score was judged or self-reported [S56]

The grader is the only thing that knows whether something judged the answer or
the learner reported on it, so `GradeResult` carries the distinction and the
`Observation` built from it inherits it. `AnswerSubmit` gains no such field: a
client cannot claim its own rating was a demonstration.

Nothing branches on it yet. This is the plumbing, kept separate so the commit
that changes behaviour is only the branch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 2: The tracer branch

The correctness fix. After this commit a self-rating can no longer move a learner's ability.

**Files:**

- Modify: `app/learning/mastery.py:34-43` (`EVENT_SCHEMA_VERSION`), `app/learning/mastery.py:289-380` (`record_observation`)
- Modify: `app/services/assessment.py:505-525` (`_recorded_grade`)
- Test: `tests/test_evidence_kinds.py`

**Interfaces:**

- Consumes: `Observation.evidence_kind` and `GradeResult.evidence_kind` from Task 1.
- Produces: `app.learning.mastery.SELF_REPORT_EVENT: str = "self_report"` — the `LearningEvent.event_type` for a self-rating. Tasks 3, 4 and 5 all filter on it by this name. `EVENT_SCHEMA_VERSION == 4`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evidence_kinds.py`:

```python
async def test_a_self_rating_advances_the_schedule_and_moves_nothing_else(
    db_session: AsyncSession,
) -> None:
    """The whole point, stated once: retention yes, ability no."""
    learner, (kc,) = await _seed(db_session)
    graded = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, graded)
    ability, uncertainty = state.ability, state.uncertainty
    last_seen, due = state.last_seen_at, state.due_at

    rated = Observation(
        learner_id=learner.id,
        kc_weights={kc.id: 1.0},
        score=1.0,
        evidence_kind=EvidenceKind.SELF_REPORTED,
    )
    (after,) = await mastery.record_observation(
        db_session, rated, now=datetime.now(UTC) + timedelta(days=1)
    )

    assert after.ability == ability
    assert after.uncertainty == uncertainty
    assert after.last_seen_at == last_seen
    assert after.due_at != due  # the review schedule did move


async def test_a_graded_answer_still_moves_everything(db_session: AsyncSession) -> None:
    """The other half of the claim — without this, deleting the update would also pass."""
    learner, (kc,) = await _seed(db_session)
    first = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (state,) = await mastery.record_observation(db_session, first)
    ability, uncertainty = state.ability, state.uncertainty
    last_seen, due = state.last_seen_at, state.due_at

    second = Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0)
    (after,) = await mastery.record_observation(
        db_session, second, now=datetime.now(UTC) + timedelta(days=1)
    )

    assert after.ability != ability
    assert after.uncertainty != uncertainty
    assert after.last_seen_at != last_seen
    assert after.due_at != due


async def test_self_report_does_not_keep_a_stale_estimate_looking_fresh(
    db_session: AsyncSession,
) -> None:
    """The regression a single-rating test would miss.

    `last_seen_at` drives `elapsed_days` -> decay -> uncertainty growth. If a self-rating
    refreshed it, a learner could rate daily for a month and keep a month-old estimate
    reading as current — the same contamination as moving ability, just slower.
    """
    learner, (kc,) = await _seed(db_session)
    start = datetime.now(UTC) - timedelta(days=30)
    await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
        now=start,
    )
    for day in range(1, 30):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
            now=start + timedelta(days=day),
        )
    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None
    assert state.last_seen_at is not None
    # Still the day of the one real demonstration, not day 29.
    assert (state.last_seen_at.replace(tzinfo=UTC) - start).days == 0


async def test_a_self_rating_writes_its_own_event_type(db_session: AsyncSession) -> None:
    learner, (kc,) = await _seed(db_session)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=0.8,
            detail={"rating": 3, "method": "self"},
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    events = (
        await db_session.scalars(select(LearningEvent).where(LearningEvent.kc_id == kc.id))
    ).all()
    assert [e.event_type for e in events] == [mastery.SELF_REPORT_EVENT]
    assert events[0].payload["score"] == 0.8
    assert events[0].payload["detail"]["rating"] == 3
    # Nothing moved, so there is no prior/posterior pair to record. Asserting their absence
    # rather than their equality: a replay must not be handed a step it can "reproduce".
    assert "posterior_ability" not in events[0].payload


async def test_a_kc_whose_first_contact_is_a_flashcard_has_no_ability_evidence(
    db_session: AsyncSession,
) -> None:
    """There must still be a row — the FSRS card needs somewhere to live — but it holds the
    unknown prior and no evidence clock."""
    learner, (kc,) = await _seed(db_session)
    (state,) = await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    assert state.ability == 0.0
    assert state.uncertainty == 1.0
    assert state.last_seen_at is None
    assert state.fsrs_card is not None
    assert state.due_at is not None


async def test_a_retried_self_rating_is_recorded_once(db_session: AsyncSession) -> None:
    """The idempotency key is the attempt, not the kind of evidence it produced. Without
    `_recorded_grade` learning the new event type, a retried flashcard would re-grade, hit
    the unique index on (learner, attempt, KC) and 500 where a graded answer replays.
    """
    learner, (kc,) = await _seed(db_session)
    attempt = uuid.uuid4()
    for _ in range(2):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                attempt_id=attempt,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
        )
        await db_session.flush()
```

That last test drives the tracer directly and will raise `IntegrityError` on the second
write — which is the *correct* behaviour at that layer, and exactly what `answer_item`'s
`except IntegrityError` block exists to turn into a replay. Write it instead against
`assessment.answer_item` with a repeated `attempt_id`, asserting the second call returns the
same `GradeResult` and that only one `self_report` event per KC exists. Model it on the
existing retry test in `tests/test_assessment.py` — find it with
`grep -n "attempt_id" tests/test_assessment.py`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_evidence_kinds.py -v -k "self_rating or self_report or graded_answer or first_contact"`

Expected: FAIL. `test_a_self_rating_advances_the_schedule_and_moves_nothing_else` fails on `after.ability == ability`; the event-type test fails with `AttributeError: module 'app.learning.mastery' has no attribute 'SELF_REPORT_EVENT'`.

- [ ] **Step 3: Bump the schema version and name the event type**

In `app/learning/mastery.py`, replace the `EVENT_SCHEMA_VERSION` block (lines 34-43) with:

```python
EVENT_SCHEMA_VERSION = 4
"""Payload shape of an ``observation`` or ``self_report`` event.

1 — score/difficulty/weight/credit and the grader's verdict.
2 — adds what an exact replay needs: the estimator's configuration, the timestamp the update
    actually used, the decay gap applied, the prediction made before the answer was seen, and
    the prior and posterior either side of the update. A version-1 row can still be scored, but
    it cannot be replayed exactly — it does not say what it was computed from.
3 — adds the per-component score and the ``component_scored`` flag that says whether the
    grader distinguished the components or the item's aggregate simply landed on each (S10).
4 — splits self-rated evidence out under its own ``event_type`` (S56). A ``self_report`` row
    carries the rating and its FSRS outcome but no prior/posterior pair, because nothing about
    the ability estimate moved. Rows at versions 1-3 are all ``observation`` and are read as
    demonstrated, which is what they were recorded as.
"""

SELF_REPORT_EVENT = "self_report"
"""``LearningEvent.event_type`` for a self-rated attempt (S56).

Its own type rather than a payload flag, because every reader of this log already filters
``event_type == "observation"`` by name. That makes exclusion what a site inherits when nobody
remembers to revisit it — so forgetting one under-counts activity instead of feeding
self-report into a measurement. The sites that should keep seeing these name this constant.
"""
```

- [ ] **Step 4: Branch the tracer**

In `app/learning/mastery.py`, inside `record_observation`'s per-KC loop, replace the block running from `elapsed_days = _elapsed_days(...)` through the closing `)` of `session.add(LearningEvent(...))` with the following. The `for kc_id, raw_w in obs.kc_weights.items():` header, the `weight`/`kc_score`/`state` lines above it, and the `updated.append(state)` below it all stay exactly as they are.

```python
        if obs.evidence_kind is EvidenceKind.SELF_REPORTED:
            # Retention only (S56). The learner is reporting whether the memory came back,
            # which is precisely the signal FSRS was built on and precisely not a measurement
            # of what they can do unaided. `last_seen_at` is untouched on purpose: it is read
            # only by decay, so refreshing it here would let self-report suppress the
            # uncertainty growth that makes a stale estimate look stale.
            state.fsrs_card, state.due_at = scheduler.review(
                state.fsrs_card, score=kc_score, now=now
            )
            session.add(
                LearningEvent(
                    learner_id=obs.learner_id,
                    kc_id=kc_id,
                    event_type=SELF_REPORT_EVENT,
                    attempt_id=attempt_id,
                    payload={
                        "score": kc_score,
                        "item_score": obs.score,
                        "component_scored": obs.kc_scores is not None,
                        "difficulty": obs.difficulty,
                        "weight": weight,
                        "item_id": str(obs.item_id) if obs.item_id is not None else None,
                        "response": obs.response,
                        "latency_ms": obs.latency_ms,
                        "hints_used": obs.hints_used,
                        "prior_attempts": obs.prior_attempts,
                        "correct": obs.correct,
                        "detail": obs.detail,
                        "schema_version": EVENT_SCHEMA_VERSION,
                        "observed_at": now.isoformat(),
                        "due_at": state.due_at.isoformat() if state.due_at else None,
                    },
                )
            )
            updated.append(state)
            continue
        elapsed_days = _elapsed_days(state.last_seen_at, now)
```

...and leave the rest of the demonstrated path exactly as it was, ending at `updated.append(state)`.

Note what the self-report payload deliberately omits: `credit`, `estimator`, `estimator_config`, `elapsed_days`, `predicted`, and the four prior/posterior fields. None of them exist for this row — no estimator ran. Writing zeros or repeated priors would hand the replay miner a step it believes it can reproduce.

- [ ] **Step 5: Teach `_recorded_grade` about the new type**

In `app/services/assessment.py`, replace the `event_type` filter inside `_recorded_grade` (around line 521):

```python
                # A retried self-rating replays exactly as a graded attempt does — the
                # idempotency key is the attempt, not the kind of evidence it produced.
                LearningEvent.event_type.in_(
                    ("admin_observation",)
                    if session.info.get("admin_actor_id")
                    else ("observation", mastery.SELF_REPORT_EVENT)
                ),
```

`mastery` is already imported in this module (`from app.learning import mastery` — confirm before editing; if the import is `from app.learning.mastery import Observation` only, add `from app.learning import mastery`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_evidence_kinds.py -v`

Expected: PASS, all.

- [ ] **Step 7: Run the full gate and read the failures carefully**

Run: `uv run poe check`

Expected: green. If an existing test fails, read it before changing it. A test that asserted a self-rating moves ability was asserting the bug and should be rewritten to assert the fix; a test that broke because it expected `event_type == "observation"` for a flashcard should name `SELF_REPORT_EVENT`. **Do not** weaken an assertion to make it pass.

- [ ] **Step 8: Prove the guard is not vacuous**

Temporarily change the branch condition in `record_observation` to `if False:` and run:

Run: `uv run pytest tests/test_evidence_kinds.py -v`

Expected: FAIL — at minimum `test_a_self_rating_advances_the_schedule_and_moves_nothing_else` and `test_self_report_does_not_keep_a_stale_estimate_looking_fresh`. Restore the condition and re-run to confirm green before staging. Confirm with `git diff app/learning/mastery.py` that the mutation is gone.

- [ ] **Step 9: Commit**

```bash
git add app/learning/mastery.py app/services/assessment.py tests/test_evidence_kinds.py
git status
git commit -F - <<'EOF'
fix(learning): a self-rating updates retention, not ability [S56]

A flashcard self-rating reached the tracer on the same path as an LLM-graded
answer, so clicking "I knew that" moved measured ability, shrank its uncertainty
and refreshed its decay clock. The estimate is the product; one a learner can
move by asserting it is not a measurement.

Self-reported evidence now advances FSRS and writes its own `self_report` event,
and touches ability, uncertainty and last_seen_at not at all. last_seen_at is
the subtle one: it is read only by decay, so refreshing it would have let self-
report suppress the uncertainty growth that makes a stale estimate look stale —
the same contamination, slower and harder to see.

Its own event type rather than a payload flag, because every reader of this log
already filters on "observation" by name. Exclusion is now what a site inherits
when nobody revisits it, so forgetting one under-counts activity rather than
feeding self-report into a measurement.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 3: The sites that opt back in

Task 2 made exclusion the default. These six sites should keep seeing self-reports, and one partial index has to widen so two of them stay fast.

**Files:**

- Create: `db/migrations/versions/0057_self_report_events.py`
- Modify: `app/models/learning.py:60-70` (the `ix_learning_events_learner_item` definition)
- Modify: `app/learning/mastery.py` (`recent_attempts_at_item` ~line 407, `recent_struggle` ~line 538)
- Modify: `app/services/assessment.py:289-296` (`last_answered`)
- Modify: `app/services/notes.py:147` (`_has_new_activity`), `app/services/notes.py:741` (`latest_event`)
- Modify: `app/services/analytics.py:155` (streak/momentum)
- Test: `tests/test_evidence_kinds.py`

**Interfaces:**

- Consumes: `mastery.SELF_REPORT_EVENT` from Task 2.
- Produces: a module-level constant in `app/learning/mastery.py` used by every site that includes both kinds:

```python
ATTEMPT_EVENTS: tuple[str, str] = ("observation", SELF_REPORT_EVENT)
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evidence_kinds.py`:

```python
async def test_a_self_rating_counts_as_having_seen_the_item(db_session: AsyncSession) -> None:
    """Re-asks are re-asks whoever marked them: the question is "have they just seen this",
    and a learner who rated a card five minutes ago has."""
    learner, (kc,) = await _seed(db_session)
    item_id = uuid.uuid4()
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            item_id=item_id,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    count = await mastery.recent_attempts_at_item(db_session, learner.id, item_id)
    assert count == 1


async def test_repeated_low_self_ratings_still_read_as_struggle(
    db_session: AsyncSession,
) -> None:
    """A self-rating may ask for help even though it may not make a claim. Three "Again"s
    are a learner saying they are stuck, and a detour is help, not a measurement."""
    learner, (kc,) = await _seed(db_session)
    now = datetime.now(UTC)
    for day in range(3):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=0.2,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
            now=now + timedelta(days=day),
        )
    struggle = await mastery.recent_struggle(db_session, learner.id, kc.id, threshold=0.5)
    assert struggle.consecutive_failures == 3
```

Add a test for streak/momentum in `tests/test_analytics.py`, following that file's existing fixtures:

```python
async def test_flashcard_reviews_count_as_activity(db_session: AsyncSession) -> None:
    """Reviewing flashcards is showing up. Streak and momentum measure effort, not evidence,
    so they are one of the few places self-report belongs."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            event_type=mastery.SELF_REPORT_EVENT,
            attempt_id=uuid.uuid4(),
            payload={"score": 1.0},
        )
    )
    await db_session.flush()

    result = await svc.get_activity(db_session, learner.id)
    assert result.observations_last_7d == 1
    assert result.streak_days == 1
```

`svc` is `app.services.analytics` in that file and `get_activity` is the real function name;
add `from app.learning import mastery` to its imports.

Add the notes activity probe's test to `tests/test_notes_service.py`:

```python
async def test_a_flashcard_review_makes_a_topic_worth_redistilling(
    db_session: AsyncSession,
) -> None:
    """The probe that decides *whether* to distill counts a review, even though the sample
    the model is later shown does not (see the exclusion test)."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug="kc", name="KC")
    db_session.add(kc)
    await db_session.flush()
    watermark = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=kc.id,
            event_type=mastery.SELF_REPORT_EVENT,
            payload={"score": 1.0},
        )
    )
    await db_session.flush()

    assert await notes_svc._has_new_activity(
        db_session, learner.id, topic, watermark, watermark
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_evidence_kinds.py tests/test_analytics.py -v -k "self_rating_counts or struggle or flashcard_reviews"`

Expected: FAIL — each assertion sees `0` where it expects the self-report counted.

- [ ] **Step 3: Add the shared constant**

In `app/learning/mastery.py`, directly below `SELF_REPORT_EVENT`:

```python
ATTEMPT_EVENTS: tuple[str, str] = ("observation", SELF_REPORT_EVENT)
"""Both kinds of learner attempt, for the sites that ask a question both answer.

Used where the question is about the learner's *action* — did they just see this item, are
they stuck, did they show up — rather than about what their ability estimate rests on. The
sites that do ask the latter filter on ``"observation"`` alone, deliberately.
"""
```

- [ ] **Step 4: Widen the six queries**

In each location, replace `LearningEvent.event_type == "observation"` with
`LearningEvent.event_type.in_(mastery.ATTEMPT_EVENTS)` (inside `mastery.py` itself, `ATTEMPT_EVENTS` unqualified), and add a one-line comment saying why that site includes both:

| File | Function | Comment to add |
|---|---|---|
| `app/learning/mastery.py` | `recent_attempts_at_item` | `# Both kinds: "have they just seen this question" is true whoever marked it.` |
| `app/learning/mastery.py` | `recent_struggle` | `# Both kinds: a run of "Again" is a learner asking for help, which is not a claim.` |
| `app/services/assessment.py` | `last_answered` | `# Both kinds: an item they rated yesterday is not a fresh question today.` |
| `app/services/notes.py` | `_has_new_activity` | `# Both kinds: reviewing flashcards is new activity worth distilling from.` |
| `app/services/notes.py` | `latest_event` (line ~741) | `# Both kinds: this is "when did anything last happen in this topic".` |
| `app/services/analytics.py` | streak/momentum query | `# Both kinds: streak and momentum measure effort, not evidence.` |

Leave `app/services/notes.py`'s `_gather` (line ~300) on `"observation"` alone — Task 5 pins that exclusion.

Import `mastery` in `app/services/notes.py` and `app/services/analytics.py` if not already present; both already import from `app.learning`, so check first.

- [ ] **Step 5: Write the migration**

Create `db/migrations/versions/0057_self_report_events.py`:

```python
"""Let the item-lookup index cover self-rated attempts too (S56).

``ix_learning_events_learner_item`` answers "has this learner answered this item, and when",
asked once per candidate item every time practice picks a question. It was partial on
``event_type = 'observation'``; self-rated attempts now write ``'self_report'`` and the same
question is asked of them, so without this the lookup falls back to a scan of the learner's
whole history.

Index only — no data migration. Existing rows keep the ``observation`` type and are read as
demonstrated, which is what they were recorded as.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0057_self_report_events"
down_revision: str | Sequence[str] | None = "0056_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_learning_events_learner_item", table_name="learning_events")
    op.create_index(
        "ix_learning_events_learner_item",
        "learning_events",
        ["learner_id", "(payload ->> 'item_id')"],
        postgresql_where="event_type IN ('observation', 'self_report')",
    )


def downgrade() -> None:
    op.drop_index("ix_learning_events_learner_item", table_name="learning_events")
    op.create_index(
        "ix_learning_events_learner_item",
        "learning_events",
        ["learner_id", "(payload ->> 'item_id')"],
        postgresql_where="event_type = 'observation'",
    )
```

If Alembic rejects the raw expression in the column list, use `sa.text("(payload ->> 'item_id')")` — mirror exactly how `app/models/learning.py` expresses it.

- [ ] **Step 6: Update the model to match**

In `app/models/learning.py`, change the `postgresql_where` of `ix_learning_events_learner_item` to:

```python
            postgresql_where=text("event_type IN ('observation', 'self_report')"),
```

and extend the comment above it: `# Both event types, because a self-rated attempt answers the same question (S56).`

The model and the migration must agree or the next autogenerate will produce a spurious diff.

- [ ] **Step 7: Run the migration and the tests**

```bash
uv run poe db-upgrade
uv run pytest tests/test_evidence_kinds.py tests/test_analytics.py tests/test_notes_service.py -v
```

Expected: migration applies cleanly; tests PASS.

Then check the downgrade actually works — a migration whose `downgrade` was never run is a migration with an untested half:

```bash
uv run poe db-downgrade && uv run poe db-upgrade
```

- [ ] **Step 8: Run the full gate**

Run: `uv run poe check && uv run poe format-check`

- [ ] **Step 9: Commit**

```bash
git add db/migrations/versions/0057_self_report_events.py app/models/learning.py \
        app/learning/mastery.py app/services/assessment.py app/services/notes.py \
        app/services/analytics.py tests/test_evidence_kinds.py tests/test_analytics.py
git status
git commit -F - <<'EOF'
feat(learning): let the effort and help questions see self-ratings [S56]

Splitting self-report into its own event type made exclusion the default, which
is the right default but the wrong answer in six places. "Have they just seen
this item", "are they stuck", and "did they show up" are all questions a self-
rating answers: the first two are about the learner's action and the third is
about effort, and none of them is a claim about ability.

Struggle detection is the interesting one. A run of "Again" is a learner asking
for help, and a prerequisite detour is help — so it keeps listening, even though
the same run no longer moves the estimate.

The item-lookup index was partial on event_type = 'observation', so two of these
would have fallen back to scanning the learner's whole history. Its predicate
widens to match. Index only; no data migration.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 4: Evidence counts, and what `assessed` means

`kc_evidence` says how well supported an estimate is, and it gates goal claims in the next slice. It must not count a self-rating as backing — but it must not hide it either.

This task also fixes the consequence found during spec review: `assessed` is derived from the existence of a state row, and after Task 2 a flashcard-only KC has one.

**Files:**

- Modify: `app/learning/mastery.py:792-880` (`KCEvidence`, `kc_evidence`)
- Modify: `app/services/analytics.py:31-34` (`_NO_EVIDENCE`), `app/services/analytics.py:66-75` (`assessed`)
- Modify: `app/schemas/analytics.py:8-30` (`KCMasteryRead`)
- Test: `tests/test_evidence_kinds.py`, `tests/test_analytics.py`

**Interfaces:**

- Consumes: `mastery.SELF_REPORT_EVENT`, `mastery.ATTEMPT_EVENTS`.
- Produces: `KCEvidence.self_reported_attempts: int` and `KCMasteryRead.self_reported_attempts: int = 0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_evidence_kinds.py`:

```python
async def test_self_ratings_are_counted_beside_the_evidence_not_inside_it(
    db_session: AsyncSession,
) -> None:
    """The assertion that stops a self-rating being presented as backing for an estimate."""
    learner, (kc,) = await _seed(db_session)
    for _ in range(3):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                item_id=uuid.uuid4(),
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
        )
    evidence = await mastery.kc_evidence(db_session, learner.id, [kc.id])
    assert evidence[kc.id].attempts == 0
    assert evidence[kc.id].distinct_items == 0
    assert evidence[kc.id].unassisted_items == 0
    assert evidence[kc.id].self_reported_attempts == 3
    assert evidence[kc.id].transfer_shown is False
```

Append to `tests/test_analytics.py`, matching that file's existing seeding helpers:

```python
async def test_a_flashcard_only_component_is_not_reported_as_assessed(
    db_session: AsyncSession,
) -> None:
    """A flashcard-only KC has a state row so its FSRS card has somewhere to live. Reading
    that row as "assessed" would render the unknown prior as a confident-looking 50% for a
    component nobody has ever been measured on — exactly what the flag exists to prevent.
    """
    learner, subject, kc = await _seed_subject_with_one_kc(db_session)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    read = await analytics.subject_mastery(db_session, learner.id, subject.id)
    kc_read = read.topics[0].kcs[0]
    assert kc_read.assessed is False
    assert kc_read.self_reported_attempts == 1
```

Write `_seed_subject_with_one_kc` as a local helper in that file if no equivalent exists, following the seeding style already used there.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_evidence_kinds.py tests/test_analytics.py -v -k "beside_the_evidence or flashcard_only"`

Expected: FAIL — `AttributeError: 'KCEvidence' object has no attribute 'self_reported_attempts'`, and `assessed` is `True`.

- [ ] **Step 3: Add the field to `KCEvidence`**

In `app/learning/mastery.py`, add to the `KCEvidence` class body after `span_days`:

```python
    # Self-rated attempts at this component (S56). Counted separately rather than folded in:
    # a rating is not backing for the estimate, but it is not nothing either, and an
    # interaction that vanished from the summary would make the history a lie of omission.
    self_reported_attempts: int
```

- [ ] **Step 4: Count both in the query, separately**

In `kc_evidence`, widen the `where` to `LearningEvent.event_type.in_(ATTEMPT_EVENTS)` and make every existing aggregate count demonstrated rows only, adding one new aggregate for self-reports. Define alongside the existing `unassisted` expression:

```python
    demonstrated = LearningEvent.event_type == "observation"
    self_rated = LearningEvent.event_type == SELF_REPORT_EVENT
```

Then wrap each existing aggregate in `case((demonstrated, ...))` and add:

```python
            func.count(distinct(case((self_rated, attempt_key)))),
```

where `attempt_key` is the `func.coalesce(LearningEvent.attempt_id, LearningEvent.id)` expression the `attempts` aggregate already uses. Unpack the extra column in the row loop and pass `self_reported_attempts=int(self_reported or 0)` into the `KCEvidence(...)` construction.

Both `span` endpoints (`func.min(when)` and `func.max(case((unassisted, when)))`) must also be restricted to demonstrated rows — a span that starts at a self-rating would report retention the learner never demonstrated.

- [ ] **Step 5: Update `_NO_EVIDENCE` and the read schema**

In `app/services/analytics.py`:

```python
_NO_EVIDENCE = mastery.KCEvidence(
    kc_id=uuid.UUID(int=0),
    attempts=0,
    distinct_items=0,
    unassisted_items=0,
    span_days=None,
    self_reported_attempts=0,
)
```

In `app/schemas/analytics.py`, add to `KCMasteryRead` after `unassisted_items`:

```python
    # Self-rated reviews of this component (S56). Shown beside the demonstrated counts, never
    # added to them: a rating says the memory came back, not that the learner can do the thing.
    self_reported_attempts: int = 0
```

Populate it wherever `KCMasteryRead(...)` is constructed in `subject_mastery`, from `_ev(evidence, kc.id).self_reported_attempts`.

- [ ] **Step 6: Re-ground `assessed`**

In `app/services/analytics.py`'s `subject_mastery`, add to the `assessed` subquery's `where` clause:

```python
                .where(
                    LearnerKCState.learner_id == learner_id,
                    Topic.subject_id == subject_id,
                    # Ability evidence, not merely a row. A flashcard-only component has a
                    # state row so its FSRS card has somewhere to live, and `last_seen_at` is
                    # now exactly "when we last had ability evidence" (S56) — so it is the
                    # honest test for a flag that decides whether to show a number at all.
                    LearnerKCState.last_seen_at.is_not(None),
                )
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_evidence_kinds.py tests/test_analytics.py -v`

Expected: PASS.

- [ ] **Step 8: Run the full gate including the API contract**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

`api-contract` matters here: `KCMasteryRead` is a response model. The new field has a default, so the change is additive — if the contract check reports a breaking change, stop and read it rather than regenerating the snapshot.

- [ ] **Step 9: Commit**

```bash
git add app/learning/mastery.py app/services/analytics.py app/schemas/analytics.py \
        tests/test_evidence_kinds.py tests/test_analytics.py
git status
git commit -F - <<'EOF'
fix(analytics): stop a self-rating reading as backing for an estimate [S56]

kc_evidence says what a mastery estimate rests on — attempts, distinct items,
unassisted items, the span they cover — and it gates goal claims in the next
slice. A self-rating counted there inflates the apparent support for an estimate
it did not contribute to. It gets its own count instead, so the interaction is
visible beside the evidence rather than folded into it.

The `assessed` flag needed the same correction, and this one was user-visible.
It was derived from the existence of a LearnerKCState row, which was sound while
only a graded answer could create one. A flashcard-only component now has a row
so its FSRS card has somewhere to live, and would have reported assessed=true at
the unknown prior — a confident-looking 50% for something nobody has ever been
measured on, which is precisely what the flag exists to prevent. It now reads
last_seen_at, which since the tracer split means exactly "we have had ability
evidence here".

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 5: Pin the exclusions

Three consumers already exclude self-reports, purely because Task 2 chose an event type over a payload flag. That is correct behaviour nobody wrote a line for, which makes it exactly the kind of behaviour that regresses silently. This task adds no production code — only the tests that hold it.

**Files:**

- Test: `tests/test_evidence_kinds.py`

**Interfaces:**

- Consumes: `mastery.SELF_REPORT_EVENT`.

- [ ] **Step 1: Write the tests**

Append to `tests/test_evidence_kinds.py`:

```python
def test_the_profile_estimators_ignore_self_rated_attempts() -> None:
    """Every profile dimension reads `score` or `difficulty` — optimal challenge, pace,
    cognitive load, error types. Inferring "this learner thrives at difficulty 0.7" from
    scores the learner assigned themselves is circular.

    No production code implements this: it falls out of self-report having its own event
    type. That is exactly why it needs a test — behaviour nobody wrote is behaviour nobody
    notices breaking.
    """
    from app.learning.profile_estimators import _observations

    learner_id = uuid.uuid4()
    events = [
        LearningEvent(
            learner_id=learner_id,
            event_type="observation",
            attempt_id=uuid.uuid4(),
            payload={"score": 1.0},
        ),
        LearningEvent(
            learner_id=learner_id,
            event_type=mastery.SELF_REPORT_EVENT,
            attempt_id=uuid.uuid4(),
            payload={"score": 1.0},
        ),
    ]
    assert [e.event_type for e in _observations(events)] == ["observation"]


async def test_the_replay_miner_skips_self_rated_steps(db_session: AsyncSession) -> None:
    """A self-report row records no estimator, no prior and no posterior, because none ran.
    A replay handed one would be reproducing a step that never happened."""
    from tests.eval.datasets.mine import mine_observation_sequences

    learner, (kc,) = await _seed(db_session)
    for _ in range(4):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
        )
    dataset = await mine_observation_sequences(db_session, min_length=3)
    assert all(str(kc.id) != seq.kc_id for seq in dataset.sequences)
```

Add the notes outcome-sample test to `tests/test_notes_service.py`:

```python
async def test_the_distilled_note_does_not_source_outcomes_from_self_ratings(
    db_session: AsyncSession,
) -> None:
    """A note telling a learner "you struggled with photosynthesis" on the strength of their
    own rating misrepresents them to themselves. The probe that decides *whether* to distill
    still counts the review (see test_a_flashcard_review_makes_a_topic_worth_redistilling);
    the sample the model is shown does not."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    db_session.add_all([learner, subject])
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug="kc", name="KC")
    db_session.add(kc)
    await db_session.flush()
    watermark = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    for event_type in ("observation", mastery.SELF_REPORT_EVENT):
        db_session.add(
            LearningEvent(
                learner_id=learner.id,
                kc_id=kc.id,
                event_type=event_type,
                attempt_id=uuid.uuid4(),
                payload={"score": 0.2},
            )
        )
    await db_session.flush()

    gathered = await notes_svc._gather(db_session, learner.id, topic, watermark, watermark)
    assert [e.event_type for e in gathered.events] == ["observation"]
```

`_Gathered`'s events attribute may not be named `events` — read `app/services/notes.py:282-320`
and use the real attribute name.

And pin the one row of the table that needs no change at all:

```python
async def test_a_self_rating_contributes_no_diagnosis(db_session: AsyncSession) -> None:
    """`grade_flashcard` returns no diagnoses, so prior_failure_kinds finds nothing to count
    either way. Pinned because that is a property of the grader, not of this slice: if a
    flashcard ever gains a diagnosis, self-report starts feeding the failure-kind counts that
    pick teaching moves, and this test is what says so out loud.
    """
    learner, (kc,) = await _seed(db_session)
    await mastery.record_observation(
        db_session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=0.2,
            evidence_kind=EvidenceKind.SELF_REPORTED,
        ),
    )
    assert await mastery.prior_failure_kinds(db_session, learner.id, [kc.id]) == {}
```

- [ ] **Step 2: Prove each test is not vacuous**

A test asserting behaviour that already holds proves nothing unless you check it fails when the behaviour is removed. For each of the three, temporarily broaden the relevant filter to include `SELF_REPORT_EVENT`, run the test, confirm FAIL, then restore:

```bash
# profile_estimators._observations: change the event_type check to accept both, run:
uv run pytest tests/test_evidence_kinds.py::test_the_profile_estimators_ignore_self_rated_attempts -v
# expect FAIL, then restore
```

Repeat for `mine.py`'s `event_type.in_(...)` and `notes.py`'s `_gather`. Confirm with `git diff` that all three mutations are gone before staging.

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_evidence_kinds.py tests/test_notes_service.py -v`

Expected: PASS.

- [ ] **Step 4: Run the full gate**

Run: `uv run poe check && uv run poe format-check`

- [ ] **Step 5: Commit**

```bash
git add tests/test_evidence_kinds.py tests/test_notes_service.py
git status
git commit -F - <<'EOF'
test(learning): pin the exclusions nobody had to write [S56]

The profile estimators, the replay miner and the distilled-note outcome sample
all stopped seeing self-ratings the moment those got their own event type. No
line of production code says so, which is what makes it fragile: correct
behaviour that nobody implemented is correct behaviour nobody notices breaking.

Each test was checked by broadening the filter it guards and confirming it
fails, so none of them is asserting a coincidence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 6: Make a flashcard answerable

The crash. Review steps default to a flashcard; the workflow answers every item with `{"text": ...}`; `grade_flashcard` wants a rating and raises `SelfGradeError`, which `workflow.py` does not handle. This is why the event log holds zero self-ratings.

**Files:**

- Modify: `app/schemas/chat.py:131-152` (`ChatTurnRequest`)
- Modify: `app/api/v1/chat.py:297-310` (the workflow branch)
- Modify: `app/services/workflow.py:87-99` (signature), `app/services/workflow.py:131-132` (the resume `Command`)
- Modify: `app/agent/state.py` (`WorkflowState`)
- Modify: `app/agent/workflow.py:73-77` (`await_response`), `app/agent/workflow.py:79-97` (`grade`)
- Modify: `app/services/session_runner.py:268-290` (`review_item_type` docstring)
- Test: `tests/test_workflow.py` (or the file that already exercises the workflow graph — find it with `grep -rl "run_workflow_turn\|WorkflowState" tests/`)

**Interfaces:**

- Consumes: `EvidenceKind`, `mastery.SELF_REPORT_EVENT`.
- Produces: `ChatTurnRequest.rating: int | None = None`; `run_workflow_turn(..., rating: int | None = None)`; `WorkflowState["rating"]`.

- [ ] **Step 1: Write the failing test**

In `tests/test_agent_workflow.py`. It already has `_learner_and_item`, `_state` and the
`Command`-driven graph pattern; add a flashcard variant of the first and use the rest as-is:

```python
async def _learner_and_flashcard(session: AsyncSession) -> tuple[Learner, Item]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="photosynthesis", name="Photosynthesis")
    session.add(kc)
    await session.flush()
    item = await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.FLASHCARD,
            stem="What does photosynthesis produce?",
            answer_key={"back": "Sugars and oxygen."},
            kcs=[ItemKCRef(kc_id=kc.id)],
        ),
        owner_learner_id=learner.id,
    )
    return learner, item


async def test_a_flashcard_can_be_answered_with_a_rating(db_session: AsyncSession) -> None:
    """Regression for the crash this slice exists to make safe.

    Review steps default to a flashcard, and the workflow answered every item with
    {"text": ...}. grade_flashcard wants a rating, so it raised SelfGradeError — and
    workflow.py has no exception handling at all. A flashcard was unanswerable in guided
    practice, which is the real reason the event log holds no self-ratings.
    """
    learner, item = await _learner_and_flashcard(db_session)
    script = [FakeTurn(text=PRESENT), FakeTurn(text=RESPOND_2)]
    graph = build_workflow_graph(fake_llm_client(script=script), db_session, learner_id=learner.id)
    config = workflow_config("t-flashcard")

    await graph.ainvoke(_state(item), config)
    await graph.ainvoke(Command(resume={"response_text": "", "rating": 3}), config)

    events = (
        await db_session.scalars(
            select(LearningEvent).where(LearningEvent.learner_id == learner.id)
        )
    ).all()
    assert [e.event_type for e in events] == [mastery.SELF_REPORT_EVENT]
    assert events[0].payload["detail"]["rating"] == 3


async def test_a_flashcard_answered_in_prose_is_re_asked_not_guessed(
    db_session: AsyncSession,
) -> None:
    """Inventing a rating would write self-reported evidence the learner never gave, and a
    default of "Again" would punish them for typing instead of clicking."""
    learner, item = await _learner_and_flashcard(db_session)
    script = [FakeTurn(text=PRESENT), FakeTurn(text=RESPOND_1)]
    graph = build_workflow_graph(fake_llm_client(script=script), db_session, learner_id=learner.id)
    config = workflow_config("t-flashcard-prose")

    await graph.ainvoke(_state(item), config)
    await graph.ainvoke(Command(resume={"response_text": "I think sugars?"}), config)

    events = (
        await db_session.scalars(
            select(LearningEvent).where(LearningEvent.learner_id == learner.id)
        )
    ).all()
    assert events == []
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("await_response",)
```

Add `from app.learning import mastery` to that file's imports. If `create_item` rejects an
`answer_key` on a flashcard, construct the `Item` directly with `ItemKC` as
`tests/test_assessment_privacy.py` does.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_workflow.py -v -k flashcard`

Expected: FAIL with `SelfGradeError: flashcard response needs 'rating' (1-4) or 'recalled' (bool)`.

- [ ] **Step 3: Add the request field**

In `app/schemas/chat.py`, add to `ChatTurnRequest` after `satisfied`:

```python
    # The learner's self-rating for a flashcard they have just revealed (1=Again … 4=Easy),
    # following `satisfied` above: a structured reply to a structured question, not prose the
    # server has to parse. Ignored unless the paused workflow is holding a flashcard.
    rating: int | None = Field(default=None, ge=1, le=4)
```

- [ ] **Step 4: Thread it to the graph**

`app/api/v1/chat.py`, in the `TurnFlow.WORKFLOW` branch, add `rating=data.rating,` to the `run_workflow_turn(...)` call.

`app/services/workflow.py`, add `rating: int | None = None,` to the keyword-only parameters of `run_workflow_turn`, and change the resume:

```python
    if resume:
        run_input = Command(resume={"response_text": user_content, "rating": rating})
```

`app/agent/state.py`, add to `WorkflowState`:

```python
    # The learner's flashcard self-rating for this round, when they gave one (1-4). NotRequired
    # because it is genuinely absent on every non-flashcard round and on checkpoints written
    # before this field existed.
    rating: NotRequired[int | None]
```

`app/agent/workflow.py`, in `await_response`:

```python
    async def await_response(state: WorkflowState) -> dict[str, Any]:
        reply = interrupt({"prompt": state["last_message"], "round": state["rounds"] + 1})
        response_text = reply.get("response_text", "")
        messages = [*state["messages"], ChatMessage(role=ChatRole.USER, content=response_text)]
        return {
            "messages": messages,
            "response_text": response_text,
            "rating": reply.get("rating"),
        }
```

- [ ] **Step 5: Build the submission by item type**

In `app/agent/workflow.py`'s `grade`, replace the `AnswerSubmit(...)` argument with a value computed just above the `answer_item` call, after `item` is known to be non-`None`:

```python
        # A flashcard is graded by the learner's own rating, so it needs the rating — not the
        # prose the other item types are graded from. This is the only node that knows the
        # item's type, which is why the branch lives here rather than in the service.
        if ItemType(item.item_type) in SELF_GRADABLE:
            if state.get("rating") is None:
                # No rating means the learner replied in prose to a card that asks for one.
                # Re-ask rather than guess: inventing a rating would write self-reported
                # evidence the learner never gave.
                return {"rounds": state["rounds"], "awaiting_rating": True}
            response = {"rating": state["rating"]}
        else:
            response = {"text": state["response_text"]}
```

and pass `AnswerSubmit(response=response, hints_used=state["rounds"])`.

Then wire `awaiting_rating` so the graph re-presents rather than grading: add `awaiting_rating: NotRequired[bool]` to `WorkflowState`, and route back to `await_response` when it is set. Read the graph's existing conditional edges (`route_after_respond` and friends) and follow that pattern exactly — do not add a second routing style.

Import `SELF_GRADABLE` and `ItemType` from `app.models.assessment` in `app/agent/workflow.py`.

- [ ] **Step 6: Run the test**

Run: `uv run pytest tests/test_workflow.py -v -k flashcard`

Expected: PASS.

- [ ] **Step 7: Correct the obsolete docstring**

In `app/services/session_runner.py`, `review_item_type`'s docstring currently justifies switching a struggling KC to an open question with: *"a run of low self-ratings drives the ability estimate down while recording nothing at all about why"*. After Task 2 a run of low self-ratings drives nothing down. Rewrite that paragraph to the argument that survives:

```
    So a component that has failed its last few reviews is served an open question instead.
    A self-rating is the right instrument for ordinary spaced repetition and the wrong one
    here: it records whether the memory came back and nothing about *why* it did not, and
    "why" is what decides whether the answer is a notation slip, a missing prerequisite, or a
    genuine misconception. An open question is the one format the grader can diagnose and
    split by component — and asking the learner to produce the answer rather than rate their
    recall of it is also the stronger retention measure (S14).
```

Keep the rest of the docstring. The behaviour does not change; only the reason given for it, which had been resting on a mechanism this slice removed.

- [ ] **Step 8: Run the full gate**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

- [ ] **Step 9: Commit**

```bash
git add app/schemas/chat.py app/api/v1/chat.py app/services/workflow.py app/agent/state.py \
        app/agent/workflow.py app/services/session_runner.py tests/test_workflow.py
git status
git commit -F - <<'EOF'
fix(workflow): a flashcard can be answered [S54]

Review steps default to a flashcard, the workflow answered every item with
{"text": ...}, and grade_flashcard wants a rating — so it raised SelfGradeError,
which workflow.py does not handle and no test covered. Flashcards have been
unanswerable in guided practice, which is the real reason the event log holds no
self-ratings.

The rating travels as structured data on the turn request, following the
`satisfied` field the refinement graph already uses. Parsing "good" out of a
chat message was the alternative: a rating is a choice among four, not a
sentence, and a learner writing prose would have been silently scored. A round
that arrives without one re-asks rather than guessing, because inventing a
rating writes self-reported evidence the learner never gave.

Also rewrites review_item_type's docstring, which justified itself by saying a
run of low self-ratings drives the estimate down. It no longer does. The
behaviour is still right for the other half of its argument — an open question
is the only format that can diagnose — so that is the half it now gives.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 7: Reveal the answer, server-side

`public_presentation` withholds a flashcard's `back`, and must keep doing so — shipping the answer with the question and hiding it in the client puts it one devtools panel away and makes the reveal theatre.

**Files:**

- Modify: `app/api/v1/assessment.py`
- Test: `tests/test_assessment_privacy.py`

**Interfaces:**

- Produces: `POST /api/v1/items/{item_id}/reveal` → `{"back": str}`. 422 for a non-flashcard item; 404 for an item the learner cannot reach.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_assessment_privacy.py`:

This file already has `_curriculum(session, owner=None)` and the `db_session, api_client,
api_learner` fixture trio. Use them:

```python
async def _flashcard_for(session, owner, back="Sugars and oxygen."):
    subject, kc = await _curriculum(session, owner=owner)
    item = Item(
        item_type=ItemType.FLASHCARD,
        stem="What does photosynthesis produce?",
        answer_key={"back": back},
        owner_learner_id=owner,
    )
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id))
    await session.commit()
    return item


async def test_a_flashcard_back_is_not_shipped_with_the_question(
    db_session, api_client, api_learner
):
    """Withheld before reveal, so the reveal is a real step and not an animation."""
    item = await _flashcard_for(db_session, api_learner.id)
    r = await api_client.get(f"/api/v1/items/{item.id}")
    assert r.status_code == 200, r.text
    assert "back" not in str(r.json())


async def test_reveal_returns_the_back(db_session, api_client, api_learner):
    item = await _flashcard_for(db_session, api_learner.id)
    r = await api_client.post(f"/api/v1/items/{item.id}/reveal")
    assert r.status_code == 200, r.text
    assert r.json()["back"] == "Sugars and oxygen."


async def test_reveal_refuses_a_non_flashcard(db_session, api_client, api_learner):
    """There is nothing to reveal on an MCQ but its answer key, and that is the one thing a
    reveal endpoint must never be able to hand out."""
    subject, kc = await _curriculum(db_session, owner=api_learner.id)
    item = Item(
        item_type=ItemType.MCQ,
        stem="Pick one",
        answer_key={"choices": ["a", "b"], "correct": 0},
        owner_learner_id=api_learner.id,
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add(ItemKC(item_id=item.id, kc_id=kc.id))
    await db_session.commit()

    r = await api_client.post(f"/api/v1/items/{item.id}/reveal")
    assert r.status_code == 422, r.text


async def test_reveal_refuses_another_learners_item(db_session, api_client, api_learner):
    """Same scoping as every other item read — `get_item_for`, not a bare primary-key load."""
    from app.models.learner import Learner

    stranger = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    item = await _flashcard_for(db_session, stranger.id)

    r = await api_client.post(f"/api/v1/items/{item.id}/reveal")
    assert r.status_code == 404, r.text
```

Add `ItemKC` to the file's existing `app.models.assessment` import if it is not already there.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_assessment_privacy.py -v -k reveal`

Expected: FAIL with 404 (no such route).

- [ ] **Step 3: Add the endpoint**

In `app/api/v1/assessment.py`, beside the `answer` endpoint:

```python
class RevealRead(BaseModel):
    """A flashcard's reverse face, handed over only when the learner asks for it."""

    back: str


@router.post("/items/{item_id}/reveal", response_model=RevealRead)
async def reveal_item(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    learner: Learner = Depends(current_learner),
) -> RevealRead:
    """Serve a flashcard's answer at the moment the learner asks to see it (S54).

    A round trip rather than a field on the item, because `public_presentation` withholds the
    back on purpose: shipping it with the question and hiding it behind a button would put the
    answer one devtools panel away and make the reveal theatre. Flashcards only — every other
    item type's answer key stays withheld until the answer is submitted, and a reveal endpoint
    that could reach an MCQ's key would be the hole this module exists to prevent.
    """
    item = await svc.get_item_for(session, item_id, learner_id=learner.id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    if ItemType(item.item_type) not in SELF_GRADABLE:
        raise HTTPException(status_code=422, detail="only a flashcard has an answer to reveal")
    back = (item.answer_key or {}).get("back")
    if not isinstance(back, str) or not back:
        raise HTTPException(status_code=422, detail="this flashcard has no stored answer")
    return RevealRead(back=back)
```

Match the module's existing dependency names exactly — read the `answer_item` endpoint above it and copy its `Depends(...)` signature rather than the placeholders here if they differ.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_assessment_privacy.py -v`

Expected: PASS.

- [ ] **Step 5: Run the full gate**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

The contract check will report a new route. That is expected and additive.

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/assessment.py tests/test_assessment_privacy.py
git status
git commit -F - <<'EOF'
feat(assessment): reveal a flashcard's answer on request [S54]

A round trip rather than a field on the item. public_presentation withholds a
flashcard's back on purpose, and shipping it with the question to hide it behind
a button would put the answer one devtools panel away — the reveal would be an
animation, not a step.

Flashcards only, scoped by the same get_item_for check every other item read
uses. A reveal endpoint that could reach an MCQ's answer key would be exactly
the hole item_presentation exists to prevent, so it 422s on anything else.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Task 8: Think, reveal, rate

**Files:**

- Create: `frontend/src/components/lessons/FlashcardPanel.tsx`, `frontend/src/components/lessons/FlashcardPanel.test.tsx`
- Modify: `frontend/src/components/lessons/ItemPanel.tsx`
- Modify: `frontend/src/api/hooks.ts` (the reveal mutation), and wherever the turn request body is built, to carry `rating`

**Interfaces:**

- Consumes: `POST /items/{id}/reveal` from Task 7; `ChatTurnRequest.rating` from Task 6.
- Consumes: `ItemEvent.item_type` — already present in `frontend/src/api/sse.ts`, no change needed.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/components/lessons/FlashcardPanel.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FlashcardPanel } from "./FlashcardPanel";

const item = {
  id: "11111111-1111-1111-1111-111111111111",
  item_type: "flashcard",
  stem: "What is the derivative of sin x?",
  difficulty: 0.5,
  rubric_id: null,
  kcs: [],
};

describe("FlashcardPanel", () => {
  it("withholds the answer and the ratings until the learner reveals", () => {
    render(<FlashcardPanel item={item} onRate={vi.fn()} />);
    expect(screen.getByRole("button", { name: /reveal/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^good$/i })).not.toBeInTheDocument();
  });

  it("says what a rating actually does", async () => {
    render(<FlashcardPanel item={item} onRate={vi.fn()} reveal={async () => "cos x"} />);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    expect(await screen.findByText("cos x")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^good$/i })).toBeInTheDocument();
    // The learner is told which of the two things their click moves. Without this line the
    // rating reads as a score, which is the misunderstanding this whole slice is about.
    expect(screen.getByText(/sets when this comes back/i)).toBeInTheDocument();
  });

  it("reports the rating the learner chose", async () => {
    const onRate = vi.fn();
    render(<FlashcardPanel item={item} onRate={onRate} reveal={async () => "cos x"} />);
    await userEvent.click(screen.getByRole("button", { name: /reveal/i }));
    await userEvent.click(screen.getByRole("button", { name: /^good$/i }));
    expect(onRate).toHaveBeenCalledWith(3);
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && npx vitest run src/components/lessons/FlashcardPanel.test.tsx`

Expected: FAIL — the module does not exist.

- [ ] **Step 3: Build the panel**

Create `frontend/src/components/lessons/FlashcardPanel.tsx`. Three phases: stem only, then stem + answer + ratings. The `reveal` prop is injected so the test does not need a server; the default calls the endpoint from Task 7.

```tsx
import { useState } from "react";
import { RichText } from "../content/RichText";
import type { ItemEvent } from "../../api/sse";

/** FSRS's four grades, in the order the learner sees them. The numbers are the contract with
 * `grade_flashcard` (1=Again … 4=Easy) — see app/learning/grading.py's _RATING_SCORE. */
const RATINGS: { label: string; value: number }[] = [
  { label: "Again", value: 1 },
  { label: "Hard", value: 2 },
  { label: "Good", value: 3 },
  { label: "Easy", value: 4 },
];

/** Think, reveal, then rate (S54).
 *
 * The answer is fetched on reveal rather than shipped with the question: withholding it in
 * the client would put it one devtools panel away, and a reveal you can skip is not a step.
 *
 * The line under the buttons is load-bearing, not decoration. A self-rating moves the review
 * schedule and deliberately does not move the mastery estimate (S56), and a learner who
 * thinks they are scoring themselves is being misled about what their click does.
 */
export function FlashcardPanel({
  item,
  onRate,
  reveal,
}: {
  item: ItemEvent;
  onRate: (rating: number) => void;
  reveal?: (itemId: string) => Promise<string>;
}) {
  const [back, setBack] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onReveal() {
    setBusy(true);
    try {
      const fetcher = reveal ?? defaultReveal;
      setBack(await fetcher(item.id));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <RichText content={item.stem} className="text-base-content/90" />
      {back === null ? (
        <button type="button" className="btn btn-outline btn-sm self-start" onClick={onReveal} disabled={busy}>
          Reveal answer
        </button>
      ) : (
        <>
          <div className="border-base-300 rounded-box border p-3">
            <RichText content={back} className="text-base-content/90" />
          </div>
          <p className="text-caption text-base-content/60">How did you do?</p>
          <div className="flex flex-wrap gap-2">
            {RATINGS.map((r) => (
              <button
                key={r.value}
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => onRate(r.value)}
              >
                {r.label}
              </button>
            ))}
          </div>
          <p className="text-caption text-base-content/50">
            Sets when this comes back — not what Guru thinks you know.
          </p>
        </>
      )}
    </div>
  );
}
```

Add `defaultReveal` calling `POST /items/{id}/reveal` through the app's existing `apiFetch` helper (`frontend/src/api/client.ts`), returning `data.back`. Follow the error handling the other mutations in `frontend/src/api/hooks.ts` use.

- [ ] **Step 4: Run the tests**

Run: `cd frontend && npx vitest run src/components/lessons/FlashcardPanel.test.tsx`

Expected: PASS.

- [ ] **Step 5: Delegate from `ItemPanel`**

In `frontend/src/components/lessons/ItemPanel.tsx`, replace the bare stem render with a branch:

```tsx
        <div className="flex flex-col gap-3">
          <p className="text-caption text-base-content/60">{kc?.name ?? "…"}</p>
          {item.item_type === "flashcard" ? (
            <FlashcardPanel item={item} onRate={onRate} />
          ) : (
            <RichText content={item.stem} className="text-base-content/90" />
          )}
        </div>
```

Add `onRate` to `ItemPanel`'s props and thread it from the session view, where it sends the turn with `rating` set. Read how that view currently posts a turn and add the field to the existing request body — do not add a second request path.

- [ ] **Step 6: Prove the guard is not vacuous**

Change `item.item_type === "flashcard"` to `false`, run the ItemPanel and FlashcardPanel suites, confirm a failure names the missing rating buttons, then restore and confirm `git diff` is clean of the mutation.

- [ ] **Step 7: Run the frontend gate**

```bash
cd frontend && npx vitest run && npm run build && npm run lint
```

Expected: all green. `npm run build` is the type gate — `npx tsc --noEmit` checks nothing here.

- [ ] **Step 8: Click through it**

Start the dev servers, sign in, reach a review step, and confirm by hand: the stem appears with no answer; Reveal fetches it; the four buttons appear; clicking one advances the session; and the KC's mastery figure on the dashboard does **not** move while the due date does. This is the one check no test in this plan performs, and it is the claim the whole slice makes.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/components/lessons/FlashcardPanel.tsx \
        frontend/src/components/lessons/FlashcardPanel.test.tsx \
        frontend/src/components/lessons/ItemPanel.tsx \
        frontend/src/api/hooks.ts
git status
git commit -F - <<'EOF'
feat(frontend): think, reveal, then rate [S54]

The practice panel showed a flashcard's stem and offered nothing to do with it.
It now reveals the answer on request and takes one of four FSRS grades.

The answer arrives from the server on reveal rather than riding along with the
question, so it is not one devtools panel away, and its own component rather
than more branches inside a 51-line display panel.

The line under the buttons is the point, not decoration: a rating moves the
review schedule and deliberately does not move the mastery estimate, and a
learner who thinks they are scoring themselves has been misled about what their
click just did.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push
```

---

## Done criteria

- `uv run poe check`, `uv run poe format-check` and `uv run poe api-contract` green.
- `cd frontend && npx vitest run && npm run build && npm run lint` green.
- A flashcard answered end to end in the browser moves `due_at` and leaves `ability` untouched — confirmed by hand, not only by test.
- No PR. The one PR for this workstream comes after slice 4.

## Deliberately not in this slice

- **Immutable item/rubric/prompt provenance** (S56). Versions three artifacts and threads them through grading and replay; its own slice.
- **Declared conversational checks with explicit rubrics and difficulty targets** (S56). Its own slice.
- **Goal policy** (S01/S14/S63) and **guidance** (S11/S52) — workstream 2 slices 2 and 3.
