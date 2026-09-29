# Retention Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Arrange for the second unaided demonstration retention needs: a component with one unaided answer and no retention yet gets a cold, unseen written question in the review queue — when FSRS surfaces it (after `retention_min_days`) or at `retention_probe_days`, whichever is first — and an answer given straight after a worked example stops counting as unaided.

**Architecture:** `mastery.due_retention_checks` derives due checks from `kc_evidence` and FSRS state (nothing stored). The lesson plan merges them into its due-review list and flags those review steps `retention_check`; guided practice runs a flagged step like a `check_first` one (cold prompt, `taught_first=False`, unseen SHORT item — already how guided practice picks items). `/reviews/due` gains `kind` and the dashboard labels checks.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest; React + vitest; `poe api-types`.

**Spec:** `docs/superpowers/specs/2026-09-29-retention-checks-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check` and `uv run poe format-check`; Task 3 also `npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint` (in `frontend/`), `uv run poe api-types` + `uv run poe api-contract`. No migration.
- One tracker id per commit subject: `[S14]`. Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls. Timestamps: compare as naive UTC (`observed_at` casts to a naive `timestamp`; normalise `now` and `due_at` with one helper).

## Review Focus

1. A component whose only unaided answer is taught-first: it has no unaided demonstration now, so no check is due and nothing about it counts as retention (Task 1 test).
2. A check answered after a hint round: recorded assisted, does not count, and the check stays due (Task 2 test).
3. A component due both by FSRS and by the interval: one step, flagged; and the step closes once answered unaided, because condition 3 fails again (Task 2 test).
4. `retention_probe_days` configured below `retention_min_days`: clamped, never a check that cannot count (Task 1 test).
5. A retention check on a component the learner can no longer see: skipped like any review (Task 3, reviewer to check `_kcs_authorized` covers the merged list).

---

### Task 1: What counts, and when a check is due [S14]

**Files:**
- Modify: `app/learning/mastery.py` (`_unassisted_clause`; `KCEvidence.last_unassisted_at`; `RetentionCheck`; `due_retention_checks`), `app/core/config.py` (`retention_probe_days`)
- Test: `tests/test_retention_checks.py` (create)

**Interfaces:**
- Produces:
  ```python
  KCEvidence.last_unassisted_at: datetime | None     # naive UTC
  class RetentionCheck(BaseModel): kc_id: uuid.UUID; due_at: datetime; ability: float; uncertainty: float
  async def due_retention_checks(session, learner_id, *, now: datetime | None = None) -> list[RetentionCheck]  # soonest first
  Settings.retention_probe_days: float = 7.0
  ```

- [ ] **Step 1: Failing tests** — `tests/test_retention_checks.py`:

```python
"""A check is arranged for the second unaided demonstration retention needs (S14)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.learning import LearnerKCState
from tests.test_item_exposure import T0, _item, _kc, _learner


async def _answer(session, learner, kc, item, *, when, hints=None, taught_first=False):
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            item_id=item.id,
            hints_used=hints,
            taught_first=taught_first,
        ),
        now=when,
    )
    await session.flush()


async def _fsrs_due(session: AsyncSession, learner, kc, when: datetime) -> None:
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id
        )
    )
    assert state is not None
    state.due_at = when
    await session.flush()


async def _due(session, learner, now) -> list[uuid.UUID]:
    return [c.kc_id for c in await mastery.due_retention_checks(session, learner.id, now=now)]


async def _one_answer(session):
    learner = await _learner(session)
    _subject, kc = await _kc(session)
    await _answer(session, learner, kc, await _item(session, kc, "a"), when=T0)
    await _fsrs_due(session, learner, kc, T0 + timedelta(days=60))  # FSRS far away by default
    return learner, kc


async def test_an_answer_after_a_worked_example_is_not_unaided(db_session) -> None:
    """Review focus 1."""
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0,
                  taught_first=True)
    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()
    assert ev.unassisted_attempts == 0 and ev.last_unassisted_at is None
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_nothing_is_due_before_retention_min_days(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(hours=6))
    assert await _due(db_session, learner, T0 + timedelta(hours=12)) == []


async def test_fsrs_brings_a_check_forward_once_it_could_count(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(days=2))
    assert await _due(db_session, learner, T0 + timedelta(days=3)) == [kc.id]


async def test_the_interval_brings_a_check_without_fsrs(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    assert await _due(db_session, learner, T0 + timedelta(days=6)) == []
    [check] = await mastery.due_retention_checks(
        db_session, learner.id, now=T0 + timedelta(days=7, hours=1)
    )
    assert check.kc_id == kc.id
    assert check.due_at.replace(tzinfo=None) == (T0 + timedelta(days=7)).replace(tzinfo=None)


async def test_no_check_without_an_unaided_answer(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0, hints=2)
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_no_check_once_retention_is_shown(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"),
                  when=T0 + timedelta(days=8))
    assert await _due(db_session, learner, T0 + timedelta(days=40)) == []


async def test_a_probe_interval_below_the_minimum_is_raised_to_it(db_session, monkeypatch) -> None:
    """Review focus 4."""
    monkeypatch.setattr(get_settings(), "retention_probe_days", 0.25)
    monkeypatch.setattr(get_settings(), "retention_min_days", 2.0)
    learner, _kc_ = await _one_answer(db_session)
    assert await _due(db_session, learner, T0 + timedelta(days=1)) == []
    assert len(await _due(db_session, learner, T0 + timedelta(days=2, hours=1))) == 1
```

(Check `get_settings()` is monkeypatchable this way — follow whatever other tests do to override a setting, e.g. a `settings` fixture, if it is cached/frozen. Check `T0` is timezone-aware; `record_observation` stores `observed_at` from it.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_retention_checks.py -q` → FAIL (`last_unassisted_at` / `due_retention_checks` missing; the taught-first answer counted).

- [ ] **Step 3: Implement.**

`app/core/config.py`, after `retention_min_days`:

```python
    # How long after a component's latest unaided answer a retention check is arranged if
    # FSRS has not surfaced it first (S14). Read as at least `retention_min_days`: a check
    # sooner than that could not count. v1-arbitrary like its neighbours; S59 calibrates.
    retention_probe_days: float = 7.0
```

`app/learning/mastery.py`:

- `_unassisted_clause` gains a third condition and its docstring says why:

```python
def _unassisted_clause() -> ColumnElement[bool]:
    """An attempt made with no hints, not a re-look at the same question, and not given straight
    after a worked example of it (S14). Guided practice teaches before it asks; an answer given
    moments after being shown how is not the independent demonstration retention needs."""
    return and_(
        func.coalesce(LearningEvent.payload["hints_used"].astext.cast(Integer), 0) == 0,
        func.coalesce(LearningEvent.payload["prior_attempts"].astext.cast(Integer), 0) == 0,
        func.coalesce(LearningEvent.payload["taught_first"].astext, "false") != "true",
    )
```

- `KCEvidence` gains, after `unassisted_span_days`:

```python
    # When the latest unaided demonstration was (naive UTC), or None — what a retention check
    # is timed from (S14).
    last_unassisted_at: datetime | None = None
```

  and `kc_evidence` passes `last_unassisted_at=last_unassisted_at`.

- After `due_reviews`:

```python
class RetentionCheck(BaseModel):
    """A component due a delayed, independent check of retention (S14)."""

    kc_id: uuid.UUID
    due_at: datetime
    ability: float
    uncertainty: float


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


async def due_retention_checks(
    session: AsyncSession, learner_id: uuid.UUID, *, now: datetime | None = None
) -> list[RetentionCheck]:
    """Components owed the second unaided demonstration retention needs, soonest first.

    Due when the component has an unaided answer, has not shown retention, the latest unaided
    answer is at least ``retention_min_days`` old (sooner could not count) — and either FSRS
    has brought its review up or ``retention_probe_days`` have passed. FSRS may bring a check
    forward; the interval makes sure one arrives. Derived, never stored, so a missed check
    stays due. Stops once retention is shown: keeping it fresh afterwards is not this.
    """
    settings = get_settings()
    now_naive = _naive_utc(now or datetime.now(UTC))
    min_days = settings.retention_min_days
    probe_days = max(settings.retention_probe_days, min_days)
    states = (
        await session.scalars(select(LearnerKCState).where(LearnerKCState.learner_id == learner_id))
    ).all()
    evidence = await kc_evidence(session, learner_id, [s.kc_id for s in states])
    due: list[RetentionCheck] = []
    for state in states:
        found = evidence.get(state.kc_id)
        if (
            found is None
            or found.last_unassisted_at is None
            or found.retention_shown(min_days=min_days)
        ):
            continue
        last = _naive_utc(found.last_unassisted_at)
        earliest = last + timedelta(days=min_days)
        by_interval = last + timedelta(days=probe_days)
        fsrs = _naive_utc(state.due_at) if state.due_at is not None else None
        when = min(by_interval, max(fsrs, earliest)) if fsrs is not None else by_interval
        if when <= now_naive:
            due.append(
                RetentionCheck(
                    kc_id=state.kc_id,
                    due_at=when,
                    ability=state.ability,
                    uncertainty=state.uncertainty,
                )
            )
    return sorted(due, key=lambda c: c.due_at)
```

(`when` is the earlier of the interval and FSRS's date, never before `earliest`. Import `timedelta` if absent.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_retention_checks.py tests/test_item_exposure.py tests/test_evidence_kinds.py tests/test_workflow.py tests/test_prerequisite_detour.py -q` → PASS. If an existing test relied on a taught-first answer counting as unaided, update it to answer with `taught_first=False` (a check-first / REST answer) and ledger each such change. Then `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/mastery.py app/core/config.py tests/test_retention_checks.py
git status
git commit -m "feat(retention): an answer after a worked example is not unaided, and checks come due [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: A due check becomes a cold review step [S14]

**Files:**
- Modify: `app/learning/lesson_plan.py` (`StepDict.retention_check`, `revise_steps(retention_check_kc_ids=)`), `app/services/lesson_plan.py` (`DueReviews`, `_due_review_kc_ids`, callers, `PlanGroundingContext.retention_check`), `app/services/workflow.py` (cold start), `app/services/session_runner.py` (`_effective_item_type`)
- Test: `tests/test_retention_checks.py` (append)

**Interfaces:**
- Consumes: `due_retention_checks` (Task 1).
- Produces:
  ```python
  # app/learning/lesson_plan.py
  StepDict.retention_check: NotRequired[bool]
  def revise_steps(..., retention_check_kc_ids: Iterable[uuid.UUID] = (), ...)
  # app/services/lesson_plan.py
  class DueReviews(NamedTuple): kc_ids: list[uuid.UUID]; retention_checks: frozenset[uuid.UUID]
  PlanGroundingContext.retention_check: bool = False
  ```

- [ ] **Step 1: Failing tests** (append):

```python
from app.learning import lesson_plan as engine


def _review_steps(steps):
    return [s for s in steps if s["step_type"] == "review" and s["status"] not in ("done", "skipped")]


def test_a_due_check_is_a_flagged_review_step() -> None:
    kc = uuid.uuid4()
    steps = engine.revise_steps(
        [],
        mastered_kc_ids=[],
        due_review_kc_ids=[kc],
        retention_check_kc_ids=[kc],
        scaffolding=engine.ScaffoldingHints(None, None, None),
    )
    [step] = _review_steps(steps)
    assert step["retention_check"] is True


def test_an_ordinary_review_is_not_flagged() -> None:
    kc = uuid.uuid4()
    steps = engine.revise_steps(
        [], mastered_kc_ids=[], due_review_kc_ids=[kc],
        scaffolding=engine.ScaffoldingHints(None, None, None),
    )
    [step] = _review_steps(steps)
    assert step.get("retention_check") is False


async def test_the_plan_merges_checks_with_fsrs_reviews_once(db_session) -> None:
    """Review focus 3: due both ways is one entry, flagged."""
    from app.services import lesson_plan as plan_svc

    learner, kc = await _one_answer(db_session)
    await _fsrs_due(db_session, learner, kc, T0 + timedelta(days=2))
    due = await plan_svc._due_review_kc_ids(
        db_session, learner.id, {kc.id}, now=T0 + timedelta(days=8)
    )
    assert due.kc_ids == [kc.id] and due.retention_checks == frozenset({kc.id})


async def test_an_unaided_answer_closes_the_check(db_session) -> None:
    """Review focus 3: answered unaided, condition 3 fails again — not due."""
    learner, kc = await _one_answer(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"),
                  when=T0 + timedelta(hours=2))  # same day: no retention, but the latest moves
    assert await _due(db_session, learner, T0 + timedelta(days=7, hours=1)) == []


async def test_a_check_answered_after_a_hint_stays_due(db_session) -> None:
    """Review focus 2."""
    learner, kc = await _one_answer(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"),
                  when=T0 + timedelta(days=7, hours=2), hints=1)
    assert await _due(db_session, learner, T0 + timedelta(days=7, hours=3)) == [kc.id]
```

Plus two practice tests, written against the existing harness in `tests/test_workflow.py` (read `test_a_guided_practice_grade_is_recorded_as_taught_first` and the check-first test near it, and copy their setup):

- `test_a_retention_check_step_is_posed_cold` — a plan whose active step is a review with `retention_check: True`: the workflow's system prompt is `CHECK_FIRST_SYSTEM_PROMPT`-based and the recorded event has `taught_first` false (absent or `False`).
- `test_a_retention_check_asks_a_written_question_on_the_session_surface` — `session_runner._effective_item_type` for a review `PlanGroundingContext` with `retention_check=True` and `preferred_item_type="flashcard"` is `ItemType.SHORT`; without the flag it stays `FLASHCARD`.

(`ScaffoldingHints` field order: check its definition and construct it with keywords if positional does not fit.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_retention_checks.py tests/test_workflow.py -q` → the new tests FAIL.

- [ ] **Step 3: Implement.**

`app/learning/lesson_plan.py`:
- `StepDict` gains, after `check_first`:
  ```python
      # A review that is also a delayed, independent retention check (S14): posed cold, with
      # an unseen written question. Recomputed on every revision.
      retention_check: NotRequired[bool]
  ```
- `revise_steps` gains `retention_check_kc_ids: Iterable[uuid.UUID] = ()` (after `provisional_kc_ids`); docstring item 7 adds "and ``retention_check`` — whether a review step's KC is in ``retention_check_kc_ids`` (S14)". In the final loop, beside `check_first`:
  ```python
          step["retention_check"] = (
              step["step_type"] == "review" and step["kc_id"] in retention_checks
          )
  ```
  with `retention_checks = {str(kc_id) for kc_id in retention_check_kc_ids}` computed beside `provisional`.

`app/services/lesson_plan.py`:
- ```python
  class DueReviews(NamedTuple):
      """This subject's due reviews, soonest first, and which of them are retention checks."""

      kc_ids: list[uuid.UUID]
      retention_checks: frozenset[uuid.UUID]
  ```
- `_due_review_kc_ids(session, learner_id, subject_kc_ids, *, now=None) -> DueReviews`: FSRS reviews (as today) and `mastery.due_retention_checks(session, learner_id, now=now)`, both filtered to `subject_kc_ids`, merged by `due_at` soonest first, one entry per KC (a KC in both keeps the earlier position), `retention_checks` = the checks' KC ids. Compare `due_at` values through naive UTC.
- `_apply_revision(due_reviews: DueReviews)`; every `engine.revise_steps(...)` call there and in the generate path passes `due_review_kc_ids=due_reviews.kc_ids, retention_check_kc_ids=due_reviews.retention_checks`. `_revision_inputs` returns the `DueReviews` unchanged in its tuple.
- `PlanGroundingContext` gains `retention_check: bool = False` (comment: "A delayed retention check (S14): practice asks before it explains."), and `get_active_step_context` passes `retention_check=bool(active.get("retention_check"))`.

`app/services/workflow.py` — where `check_first` decides the start:
```python
        # Posed cold for a provisional component (S24) and for a retention check (S14): both
        # need an answer given without a worked example.
        cold = step.check_first or step.retention_check
        base_prompt = CHECK_FIRST_SYSTEM_PROMPT if cold else WORKFLOW_SYSTEM_PROMPT
```
and `"taught_first": not cold`.

`app/services/session_runner.py::_effective_item_type` — first line of the body:
```python
    if context.retention_check:
        # A self-rating can never be the unaided demonstration a retention check exists to get.
        return ItemType.SHORT
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_retention_checks.py tests/test_workflow.py tests/test_lesson_plan*.py tests/test_prerequisite_detour.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/lesson_plan.py app/services/lesson_plan.py app/services/workflow.py app/services/session_runner.py tests/test_retention_checks.py tests/test_workflow.py
git status
git commit -m "feat(retention): a due check is a review step posed cold [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The review queue says which entries are checks [S14]

**Files:**
- Modify: `app/learning/mastery.py` (`ReviewItem.kind`), `app/services/session_runner.py` (`due_review_items`), `app/schemas/assessment.py` (`ReviewItemRead.kind`), `app/api/v1/assessment.py`, `frontend/src/components/dashboard/ReviewsDueCard.tsx`, the generated API schema/types
- Test: `tests/test_retention_checks.py` (append), `frontend/src/components/dashboard/ReviewsDueCard.test.tsx` (create)

- [ ] **Step 1: Failing tests.**

Python (append):

```python
from app.llm.registry import fake_llm_client
from app.services import session_runner


async def test_the_due_list_marks_a_check_and_serves_it_a_written_question(db_session) -> None:
    learner, kc = await _one_answer(db_session)
    pairs = await session_runner.due_review_items(
        db_session, fake_llm_client(), learner_id=learner.id, item_limit=5,
        now=T0 + timedelta(days=8),
    )
    [(review, item)] = pairs
    assert review.kind == "retention_check" and review.kc_id == kc.id
    assert item is None or item.item_type == "short"
```

(If generation through `fake_llm_client()` cannot produce a SHORT item, give it the short-item JSON reply used in `tests/test_conversation_evidence.py` (`SHORT_REPLY`) and assert `item.item_type == "short"` outright.)

Frontend — `ReviewsDueCard.test.tsx`: mock `useReviewsDue` and `useKC` (follow how other dashboard tests mock hooks; if none do, `vi.mock("../../api/hooks", ...)`), render two rows — `kind: "review"` and `kind: "retention_check"` — and assert "Retention check" appears exactly once.

- [ ] **Step 2: Run** — `uv run pytest tests/test_retention_checks.py -q -k due_list` and `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/dashboard/ReviewsDueCard.test.tsx` → FAIL.

- [ ] **Step 3: Implement.**

- `ReviewItem` gains `kind: Literal["review", "retention_check"] = "review"`.
- `due_review_items(..., now: datetime | None = None)`: after `mastery.DEFAULT_TRACER.due_reviews(...)`, merge `mastery.due_retention_checks(session, learner_id, now=now)` — one entry per KC; a KC in both becomes the check (`kind="retention_check"`, the earlier `due_at`); a check-only KC becomes `ReviewItem(kc_id, due_at, ability, uncertainty, kind="retention_check")`. Sort soonest first. Keep the `_kcs_authorized` skip for every entry. A check's item is `short_answer_item_for_kc(...)` (unseen SHORT); an ordinary review keeps `review_item_type`.
- `ReviewItemRead.kind: Literal["review", "retention_check"] = "review"` (docstring line: "``retention_check`` — a delayed independent check of retention (S14): an unseen written question, answered without help."); the endpoint passes `kind=review.kind`.
- `uv run poe api-types`; stage the regenerated schema; `uv run poe api-contract`.
- `ReviewsDueCard`: in `ReviewRow`, before the due text, when `review.kind === "retention_check"` render `<span className="badge badge-sm badge-primary badge-soft">Retention check</span>` (match existing badge classes in the dashboard if they differ).

- [ ] **Step 4: Run** — the two test commands above → PASS; `uv run poe check && uv run poe format-check`; in `frontend/`: `npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/mastery.py app/services/session_runner.py app/schemas/assessment.py app/api/v1/assessment.py tests/test_retention_checks.py frontend/src/components/dashboard/ReviewsDueCard.tsx frontend/src/components/dashboard/ReviewsDueCard.test.tsx <the regenerated schema files>
git status
git commit -m "feat(retention): the review queue marks retention checks [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Docs [S14]

- [ ] **Step 1:** Tracker S14: delayed independent checks done (hybrid timing, `retention_probe_days`, taught-first answers no longer unaided); transfer remains (workstream 2, piece 4); the final review's deferred minors.
- [ ] **Step 2:** Goal-policy design §11, "Delayed independent probes (slice 2b)": append "Done: [retention checks](2026-09-29-retention-checks-design.md)."
- [ ] **Step 3:** CLAUDE.md, the "self-rating is not evidence" bullet: add "an answer given straight after a worked example is not unaided, and a component owed a second unaided answer gets a cold retention check in its review queue (S14)".
- [ ] **Step 4: Commit**

```bash
git add docs/guru-suggestions-tracker.md docs/superpowers/specs/2026-09-22-goal-policy-design.md CLAUDE.md
git status
git commit -m "docs: record delayed retention checks [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
