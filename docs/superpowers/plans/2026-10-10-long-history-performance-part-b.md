# Long-history performance, part B (S62) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every path the part A report selected (reviews_due, practice, plan_revision, profile_refresh, turn, activity) is under 250 ms p95 at the 5× power user and does not grow with history, so a rerun of `poe perf-report` selects nothing.

**Architecture:** Measure first, then fix each cause test-first. The retention and transfer checks stop reading the whole event log: three one-way milestones on `learner_kc_state` say which components could be due, and evidence is computed only for those. Plan revision asks only about its own subject. Four smaller fixes: the review queue's per-review access check becomes one query; the item-lookup index gains `created_at`, so "when did they last answer this" is one index read; activity aggregates by day in SQL; the budget test's windows are made smaller than the small history.

**Tech Stack:** Python 3.13, SQLAlchemy async, PostgreSQL 17, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-long-history-performance-design.md` (part B is §5 "Sequencing"; the selection is under "Results").

## Measured causes (guru_perf, seed v2, 2026-10-10)

The selected paths were profiled statement by statement against `guru_perf`, and the slow statements were run under `EXPLAIN (ANALYZE, BUFFERS)`.

| Path | p95 (part A) | Measured cause | Fix (task) |
| --- | ---: | --- | --- |
| reviews_due | 9.8 s | 4,364 per-review access checks (2.7 s); 10 item lookups at ~120 ms (1.2 s); evidence for all 4,500 components, read twice (retention and transfer) — the settings query 470 ms each, the evidence count 195 ms each | 1, 2, 3 |
| plan_revision | 1.9 s | the same evidence read for all 4,500 components, twice, then filtered to one subject (300) | 3, 4 |
| practice | 2.3 s | grading revises the plan (as above); 5 item lookups at ~27 ms | 2, 3, 4 |
| profile_refresh | 2.0 s | revises each plan (as above). Its growth in the budgets comes from the test: its 2,000-event and 500-message windows are bigger than the small history, so the larger history reads more only because the small one never fills them | 3, 4, 6 |
| turn | 299 ms | the item lookup (113 ms): `max(created_at)` over the learner's events for an item runs as a backward scan of `ix_learning_events_created_at` that filters all 225k rows, because `ix_learning_events_learner_item` has no `created_at` column. Probed: with `(learner_id, item_id, created_at)` the lookup takes 0.14 ms | 2 |
| activity | 136 ms (growth) | one row per event in the 90-day window (18.5k) aggregated in Python | 5 |

Two findings rejected as fixes:
- **Turning JIT off.** JIT compilation costs about 400 ms of the 431 ms settings query. That query will no longer run at that size, so the setting would only hide the cause.
- **Narrowing the query's columns and raising `work_mem`.** Measured at best 190 ms for one global evidence read, which is still a lifetime scan.

## Global Constraints

- Branch `feat/workstream-2` (PR #44). Never reset, amend, rebase, squash or force-push.
- One tracker id per commit subject: `[S62]`. Trailer exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Stage only the task's files by name, then `git status`.
- Fix rule (spec decision 3): a path is fixed when its statements or rows grow with history, or when its server-side p95 at the 5× history exceeds **250 ms**.
- Budgets in `poe check`: statements(4×) ≤ statements(1×) + 2 and rows(4×) ≤ rows(1×) + 2 (`tests/test_history_budgets.py`).
- The checks stay derived: the milestones only say which components *could* be due; retention and transfer are still decided from the event log by `kc_evidence`.
- After a model or migration change: `uv run poe db-upgrade && uv run poe db-check`.
- No paid model calls; `poe perf-report` runs on `FakeProvider` and its own database.

## Review Focus

1. **A learner whose states predate the milestones** (every existing row after the migration): the first due read must find exactly what it found before, and fill the milestones as it goes. Pinned in Task 3 (`test_unmarked_states_are_marked_on_first_read`).
2. **`retention_min_days` raised after retention was shown:** `retention_shown_at` is a cached yes, so it would go stale. The RUNBOOK says how to clear it (`evidence_marked_at = NULL`); the check is in Task 3 (`test_clearing_the_mark_recomputes_it`).
3. **Transfer candidates beyond the cap:** a power user can have thousands of retained, untransferred components. Only the soonest `due_reviews_limit` get evidence read; the rest stay due and surface as the queue drains. Pinned in Task 3 (`test_transfer_reads_evidence_only_for_the_capped_candidates`).
4. **A self-report or admin observation:** neither may move a milestone, because neither is an unaided demonstration. Pinned in Task 3 (`test_a_self_rating_moves_no_milestone`).
5. **A review queue item the learner may not see** (a foreign private subject's component) must still be skipped after batching. Pinned in Task 1 (`test_a_foreign_component_is_skipped_in_one_query`).

## File Structure

- Modify `app/services/session_runner.py` (`due_review_items`): one batched access check.
- Modify `app/services/assessment.py`: `authorized_kc_ids(session, kc_ids, learner_id) -> set[UUID]`; `_kcs_authorized` uses it.
- Modify `app/models/learning.py`: four columns on `LearnerKCState`; `ix_learning_events_learner_item` gains `created_at`.
- Create `db/migrations/versions/0077_s62_evidence_milestones.py`.
- Modify `app/learning/mastery.py`: `_mark_evidence`, and `due_retention_checks` / `due_transfer_checks` reading milestones, both taking `kc_ids`.
- Modify `app/services/lesson_plan.py` (`_due_review_kc_ids`): pass the subject's components.
- Modify `app/services/analytics.py` (`get_activity`): aggregate by day in SQL.
- Modify `tests/test_history_budgets.py`: profile windows in `small_bounds`; `PART_B` emptied.
- Tests: `tests/test_review_queue_cost.py` (new), `tests/test_evidence_milestones.py` (new), `tests/test_item_exposure.py` (extend), `tests/test_analytics.py` (extend).
- Docs: spec "Results" (part B rerun), RUNBOOK §20, tracker S62 row, CLAUDE.md (one clause).

---

### Task 1: The review queue checks access once

**Files:**
- Modify: `app/services/assessment.py:179-193`
- Modify: `app/services/session_runner.py:303-360`
- Test: `tests/test_review_queue_cost.py`

**Interfaces:**
- Produces: `async def authorized_kc_ids(session: AsyncSession, kc_ids: Collection[uuid.UUID], learner_id: uuid.UUID) -> set[uuid.UUID]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_review_queue_cost.py
"""The review queue's cost does not grow with how much is due (S62 part B)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.models.knowledge import Subject
from app.services import session_runner
from tests.history import SMALL, seed_history
from tests.querycount import count_queries


async def _learner_with_due(session: AsyncSession, topics: int) -> uuid.UUID:
    learner = Learner(handle=f"rq-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    shape = replace(SMALL, topics_per_subject=topics, events=0, vector_pool=8)
    await seed_history(session, learner.id, shape)
    # Everything due yesterday, so the queue is as long as the graph.
    await session.execute(
        update(LearnerKCState)
        .where(LearnerKCState.learner_id == learner.id)
        .values(due_at=datetime.now(UTC) - timedelta(days=1))
    )
    await session.flush()
    return learner.id


async def test_the_queue_costs_the_same_however_much_is_due(db_session: AsyncSession) -> None:
    few = await _learner_with_due(db_session, topics=1)
    many = await _learner_with_due(db_session, topics=4)

    with count_queries(db_session) as small:
        a = await session_runner.due_review_items(
            db_session, fake_llm_client(), learner_id=few, item_limit=0
        )
    with count_queries(db_session) as large:
        b = await session_runner.due_review_items(
            db_session, fake_llm_client(), learner_id=many, item_limit=0
        )

    assert len(b) > len(a) > 0
    assert len(large) <= len(small), f"small {small!r}\nlarge {large!r}"


async def test_a_foreign_component_is_skipped_in_one_query(db_session: AsyncSession) -> None:
    owner = await _learner_with_due(db_session, topics=1)
    other = await _learner_with_due(db_session, topics=1)
    # Make one of `other`'s subjects private to `owner`: its components leave other's queue.
    foreign = await db_session.scalar(select(Subject.id).where(Subject.owner_learner_id == other))
    await db_session.execute(
        update(Subject).where(Subject.id == foreign).values(owner_learner_id=owner)
    )
    await db_session.flush()

    queue = await session_runner.due_review_items(
        db_session, fake_llm_client(), learner_id=other, item_limit=0
    )

    assert queue == []
```

Read `tests/history.py` before running: if `seed_history` creates the subject as curated (`owner_learner_id` NULL) rather than owned by the learner, change the second test's arrangement to set `owner_learner_id=owner` on the subject `seed_history` returned (`SeededHistory.subject_id`) instead of selecting by owner. What it must pin is that the learner's queue drops a component whose subject belongs to someone else.

- [ ] **Step 2: Run them to verify the first fails**

Run: `uv run pytest tests/test_review_queue_cost.py -v`
Expected: `test_the_queue_costs_the_same…` FAILS (the large learner runs one access statement per due review); the foreign test PASSES (behaviour already right; it pins the batched version).

- [ ] **Step 3: Implement**

`app/services/assessment.py`:

```python
async def authorized_kc_ids(
    session: AsyncSession, kc_ids: Collection[uuid.UUID], learner_id: uuid.UUID
) -> set[uuid.UUID]:
    """The subset of ``kc_ids`` this learner may use: curated, or in a subject they own."""
    if not kc_ids:
        return set()
    allowed = await session.scalars(
        select(KC.id)
        .join(Topic)
        .join(Subject)
        .where(
            KC.id.in_(list(kc_ids)),
            or_(Subject.owner_learner_id.is_(None), Subject.owner_learner_id == learner_id),
        )
    )
    return set(allowed)


async def _kcs_authorized(
    session: AsyncSession, kc_ids: list[uuid.UUID], learner_id: uuid.UUID
) -> bool:
    return await authorized_kc_ids(session, kc_ids, learner_id) == set(kc_ids)
```

(`from collections.abc import Collection` at the top.)

`app/services/session_runner.py`, in `due_review_items`, replace the per-review check:

```python
    # One query for the whole queue (S62): checked per review, a long backlog cost a
    # statement per due component before anything was resolved.
    allowed = await assessment_svc.authorized_kc_ids(
        session, [r.kc_id for r in reviews], learner_id
    )
    results: list[tuple[ReviewItem, Item | None]] = []
    # Sequential, not gathered: item_for_kc can call session.commit() on this one shared
    # AsyncSession, and concurrent operations on a single session are unsafe.
    for review in reviews:
        if review.kc_id not in allowed:
            continue
```

- [ ] **Step 4: Run them, then the queue's neighbours**

Run: `uv run pytest tests/test_review_queue_cost.py tests/test_retention_checks.py tests/test_transfer.py tests/test_authorization.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/assessment.py app/services/session_runner.py tests/test_review_queue_cost.py
git commit -m "perf(reviews): the review queue checks access once, not once per due review [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: "When did they last answer this item" is one index read

**Files:**
- Modify: `app/models/learning.py:84-93`
- Create: `db/migrations/versions/0077_s62_evidence_milestones.py` (index part; Task 3 adds columns to the same migration)
- Test: `tests/test_item_exposure.py` (extend)

**Interfaces:**
- Produces: `ix_learning_events_learner_item` on `(learner_id, (payload ->> 'item_id'), created_at)` with the same partial predicate.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_item_exposure.py`:

```python
async def test_the_last_answer_lookup_is_served_by_the_item_index(db_session) -> None:
    """`max(created_at)` for one learner and item must be an index read, not a scan (S62).

    Without `created_at` in the index the planner walks `created_at` backwards hoping to meet
    the item early, and when the learner never answered it, it walks their whole history.
    """
    from sqlalchemy import text

    columns = (
        await db_session.execute(
            text(
                "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                "WHERE indexrelid = 'ix_learning_events_learner_item'::regclass"
            )
        )
    ).scalar_one()
    assert "learner_id, ((payload ->> 'item_id'::text)), created_at" in columns
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_item_exposure.py::test_the_last_answer_lookup_is_served_by_the_item_index -v`
Expected: FAIL — the definition has no `created_at`.

- [ ] **Step 3: Model and migration**

`app/models/learning.py`, the index becomes:

```python
        Index(
            "ix_learning_events_learner_item",
            "learner_id",
            text("(payload ->> 'item_id')"),
            # Trailing, so "when did they last answer it" is read from the index (S62). Without
            # it the planner chose a backward walk of created_at that, for an item the learner
            # never answered, covered their whole history: 113 ms at a power user's size.
            "created_at",
            postgresql_where=text("event_type IN ('observation', 'self_report')"),
        ),
```

`db/migrations/versions/0077_s62_evidence_milestones.py`:

```python
"""Index the last-answer lookup fully, and give learner_kc_state its evidence milestones (S62).

``ix_learning_events_learner_item`` gains ``created_at`` so ``max(created_at)`` for one learner
and item is an index read. The milestone columns (added by part B's second task) say which
components could owe a retention or transfer check, so the checks stop reading every event.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_s62_evidence_milestones"
down_revision: str | Sequence[str] | None = "0076_resumable_ingestion"
branch_labels = None
depends_on = None

_INDEX = "ix_learning_events_learner_item"
_WHERE = sa.text("event_type IN ('observation', 'self_report')")


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="learning_events")
    op.create_index(
        _INDEX,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')"), "created_at"],
        unique=False,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="learning_events")
    op.create_index(
        _INDEX,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')")],
        unique=False,
        postgresql_where=_WHERE,
    )
```

Update the comment above `last_answered` in `app/services/assessment.py` (`find_item_for_kc`) to say the index's trailing `created_at` is what makes each probe one read.

- [ ] **Step 4: Migrate and run**

Run: `uv run poe db-upgrade && uv run poe db-check && uv run pytest tests/test_item_exposure.py -q`
Expected: upgrade to `0077_s62_evidence_milestones`, db-check clean, tests PASS. (The test database is rebuilt from migrations by the suite's fixture; if it is not, `uv run python -m tests.testdb` first.)

- [ ] **Step 5: Commit**

```bash
git add app/models/learning.py app/services/assessment.py db/migrations/versions/0077_s62_evidence_milestones.py tests/test_item_exposure.py
git commit -m "perf(items): the last-answer lookup reads created_at from the item index [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: Retention and transfer checks read milestones, not the event log

**Files:**
- Modify: `app/models/learning.py` (`LearnerKCState`: four columns)
- Modify: `db/migrations/versions/0077_s62_evidence_milestones.py` (add/drop the columns)
- Modify: `app/learning/mastery.py` (`_mark_evidence`, `record_observation`, `due_retention_checks`, `due_transfer_checks`)
- Test: `tests/test_evidence_milestones.py`

**Interfaces:**
- Consumes: `kc_evidence(session, learner_id, kc_ids) -> dict[UUID, KCEvidence]` (unchanged; still the one definition).
- Produces:
  - `LearnerKCState.unaided_last_at`, `.retention_shown_at`, `.setting_transfer_at`, `.evidence_marked_at` (all `DateTime(timezone=True) | None`).
  - `async def _mark_evidence(session, learner_id, states: Sequence[LearnerKCState], *, now: datetime) -> None`.
  - `due_retention_checks(session, learner_id, *, kc_ids: Collection[uuid.UUID] | None = None, now=None)` and `due_transfer_checks(session, learner_id, *, kc_ids: Collection[uuid.UUID] | None = None, now=None, limit: int | None = None)` — same return types as today.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_evidence_milestones.py
"""The due checks learn which components could be due from milestones, not the event log (S62).

Retention and transfer are still decided by `kc_evidence`. The milestones on learner_kc_state
only record that a component has an unaided answer, has shown retention, or has shown
transfer — each a one-way step — so a check reads evidence only where it could matter.
"""

from datetime import timedelta

from sqlalchemy import select, update

from app.core.config import get_settings
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.assessment import EvidenceKind
from app.models.learning import LearnerKCState
from tests.querycount import count_queries
from tests.test_item_exposure import T0, _item, _kc, _learner


async def _answer(session, learner, kc, item, *, when, hints=None, self_rated=False) -> None:
    await mastery.record_observation(
        session,
        Observation(
            learner_id=learner.id,
            kc_weights={kc.id: 1.0},
            score=1.0,
            correct=True,
            item_id=item.id,
            hints_used=hints,
            evidence_kind=EvidenceKind.SELF_REPORTED if self_rated else EvidenceKind.JUDGED,
        ),
        now=when,
    )
    await session.flush()


async def _state(session, learner, kc) -> LearnerKCState:
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id
        )
    )
    assert state is not None
    return state


async def test_an_unaided_answer_records_its_time(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)

    state = await _state(db_session, learner, kc)
    assert mastery.naive_utc(state.unaided_last_at) == mastery.naive_utc(T0)
    assert state.retention_shown_at is None
    assert state.evidence_marked_at is not None


async def test_a_second_unaided_answer_later_marks_retention(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    later = T0 + timedelta(days=get_settings().retention_min_days)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"), when=later)

    assert (await _state(db_session, learner, kc)).retention_shown_at is not None


async def test_a_self_rating_moves_no_milestone(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0, self_rated=True)

    state = await _state(db_session, learner, kc)
    assert state.unaided_last_at is None
    assert state.retention_shown_at is None


async def test_retention_checks_read_no_events_once_marked(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)

    with count_queries(db_session) as q:
        due = await mastery.due_retention_checks(
            db_session, learner.id, now=T0 + timedelta(days=365)
        )

    assert [c.kc_id for c in due] == [kc.id]
    assert not [s for s in q.statements if "learning_events" in s], q


async def test_unmarked_states_are_marked_on_first_read(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
    # What every row looks like straight after the migration.
    await db_session.execute(
        update(LearnerKCState)
        .where(LearnerKCState.learner_id == learner.id)
        .values(unaided_last_at=None, retention_shown_at=None, evidence_marked_at=None)
    )
    await db_session.flush()

    due = await mastery.due_retention_checks(db_session, learner.id, now=T0 + timedelta(days=365))

    assert [c.kc_id for c in due] == [kc.id]
    state = await _state(db_session, learner, kc)
    await db_session.refresh(state)
    assert state.evidence_marked_at is not None
    assert mastery.naive_utc(state.unaided_last_at) == mastery.naive_utc(T0)


async def test_clearing_the_mark_recomputes_it(db_session) -> None:
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    later = T0 + timedelta(days=get_settings().retention_min_days)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
    await _answer(db_session, learner, kc, await _item(db_session, kc, "b"), when=later)
    # A stale "shown" (as after raising retention_min_days), cleared the way the RUNBOOK says.
    await db_session.execute(
        update(LearnerKCState)
        .where(LearnerKCState.learner_id == learner.id)
        .values(retention_shown_at=None, evidence_marked_at=None)
    )
    await db_session.flush()

    await mastery.due_retention_checks(db_session, learner.id, now=later + timedelta(days=365))

    state = await _state(db_session, learner, kc)
    await db_session.refresh(state)
    assert state.retention_shown_at is not None


async def test_transfer_reads_evidence_only_for_the_capped_candidates(
    db_session, monkeypatch
) -> None:
    learner = await _learner(db_session)
    kcs = [(await _kc(db_session))[1] for _ in range(3)]
    later = T0 + timedelta(days=get_settings().retention_min_days)
    for kc in kcs:
        await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)
        await _answer(db_session, learner, kc, await _item(db_session, kc, "b"), when=later)

    asked: list[set] = []
    real = mastery.kc_evidence

    async def spy(session, learner_id, kc_ids):
        asked.append(set(kc_ids))
        return await real(session, learner_id, kc_ids)

    monkeypatch.setattr(mastery, "kc_evidence", spy)
    await mastery.due_transfer_checks(
        db_session, learner.id, now=later + timedelta(days=365), limit=1
    )

    assert asked and all(len(ids) <= 1 for ids in asked), asked


async def test_due_checks_can_be_scoped_to_components(db_session) -> None:
    learner = await _learner(db_session)
    _s1, kc1 = await _kc(db_session)
    _s2, kc2 = await _kc(db_session)
    for kc in (kc1, kc2):
        await _answer(db_session, learner, kc, await _item(db_session, kc, "a"), when=T0)

    due = await mastery.due_retention_checks(
        db_session, learner.id, kc_ids={kc1.id}, now=T0 + timedelta(days=365)
    )

    assert [c.kc_id for c in due] == [kc1.id]
```

Before running, read `tests/test_item_exposure.py` for the real names and signatures of `T0`, `_learner`, `_kc` and `_item`, and `app/learning/mastery.py` for `Observation`'s fields (`evidence_kind`, `correct`). Adjust the helper to match; the assertions stay as written.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_evidence_milestones.py -v`
Expected: FAIL — `LearnerKCState` has no `unaided_last_at`; `due_retention_checks` takes no `kc_ids`; `due_transfer_checks` takes no `limit`.

- [ ] **Step 3: Columns and migration**

`app/models/learning.py`, on `LearnerKCState` after `transfer_confirmed_at`:

```python
    # Evidence milestones (S62). Which components *could* owe a retention or transfer check,
    # so the checks read evidence only for those instead of every event the learner has.
    # Each is a one-way step recorded from `kc_evidence` when an answer lands; the checks
    # still decide from the event log. Not to be confused with `transferred_at` above, which
    # is the cross-subject head start (S24).
    unaided_last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    retention_shown_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    setting_transfer_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # NULL until the milestones were computed for this row (every row before 0077, and any row
    # cleared on purpose); the next due read computes and stores them.
    evidence_marked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

In `0077_s62_evidence_milestones.py`, add to `upgrade()`:

```python
    for column in ("unaided_last_at", "retention_shown_at", "setting_transfer_at", "evidence_marked_at"):
        op.add_column(
            "learner_kc_state", sa.Column(column, sa.DateTime(timezone=True), nullable=True)
        )
```

and to `downgrade()` (first, before the index):

```python
    for column in ("evidence_marked_at", "setting_transfer_at", "retention_shown_at", "unaided_last_at"):
        op.drop_column("learner_kc_state", column)
```

Run `uv run poe db-upgrade` after `uv run poe db-downgrade` (one step back to 0076, then up again) so the dev database has the columns, then `uv run poe db-check`.

- [ ] **Step 4: Maintain the milestones**

`app/learning/mastery.py`, beside `_record_achievements`:

```python
async def _mark_evidence(
    session: AsyncSession,
    learner_id: uuid.UUID,
    states: Sequence[LearnerKCState],
    *,
    now: datetime,
) -> None:
    """Record each component's evidence milestones from ``kc_evidence`` (S62).

    The one definition of retention and transfer stays ``kc_evidence``; this stores its answer
    for the three questions the due checks start from. Retention and transfer, once shown, stay
    shown (more evidence cannot undo them), so a stored "yes" holds until something changes the
    definition — and then clearing ``evidence_marked_at`` makes the next read recompute it.
    """
    if not states:
        return
    min_days = get_settings().retention_min_days
    evidence = await kc_evidence(session, learner_id, [s.kc_id for s in states])
    for state in states:
        found = evidence.get(state.kc_id)
        last = found.last_unassisted_at if found is not None else None
        state.unaided_last_at = last.replace(tzinfo=UTC) if last is not None else None
        shown = found is not None and found.retention_shown(min_days=min_days)
        if shown and state.retention_shown_at is None:
            state.retention_shown_at = now
        elif not shown:
            state.retention_shown_at = None
        transferred = found is not None and found.transfer_shown
        if transferred and state.setting_transfer_at is None:
            state.setting_transfer_at = now
        elif not transferred:
            state.setting_transfer_at = None
        state.evidence_marked_at = now
```

In `record_observation`, after `_record_achievements(...)` and before the final flush:

```python
    await _mark_evidence(session, obs.learner_id, demonstrated_states, now=now)
```

(Only `demonstrated_states`: a self-report or an admin observation is not an unaided demonstration and moves no milestone.)

- [ ] **Step 5: The checks start from the milestones**

Replace `due_retention_checks`:

```python
async def _marked_states(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_ids: Collection[uuid.UUID] | None,
    *,
    now: datetime,
) -> list[LearnerKCState]:
    """The learner's states (optionally only ``kc_ids``), milestones filled in where missing."""
    stmt = select(LearnerKCState).where(LearnerKCState.learner_id == learner_id)
    if kc_ids is not None:
        stmt = stmt.where(LearnerKCState.kc_id.in_(list(kc_ids)))
    states = list((await session.scalars(stmt)).all())
    unmarked = [s for s in states if s.evidence_marked_at is None]
    if unmarked:
        await _mark_evidence(session, learner_id, unmarked, now=now)
        await session.flush()
    return states


async def due_retention_checks(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    kc_ids: Collection[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> list[RetentionCheck]:
    """(docstring unchanged, plus:) Read from the milestones (S62): a component with an unaided
    answer and no retention shown is a candidate, and its dates are all on the state row, so no
    event is read once the row is marked. ``kc_ids`` limits the read to those components."""
    settings = get_settings()
    now_aware = now or datetime.now(UTC)
    now_naive = naive_utc(now_aware)
    min_days = settings.retention_min_days
    probe_days = max(settings.retention_probe_days, min_days)
    due: list[RetentionCheck] = []
    for state in await _marked_states(session, learner_id, kc_ids, now=now_aware):
        if state.unaided_last_at is None or state.retention_shown_at is not None:
            continue
        last = naive_utc(state.unaided_last_at)
        earliest = last + timedelta(days=min_days)
        by_interval = last + timedelta(days=probe_days)
        fsrs = naive_utc(state.due_at) if state.due_at is not None else None
        when = min(by_interval, max(fsrs, earliest)) if fsrs is not None else by_interval
        if when <= now_naive:
            due.append(
                RetentionCheck(
                    kc_id=state.kc_id,
                    due_at=when.replace(tzinfo=UTC),
                    ability=state.ability,
                    uncertainty=state.uncertainty,
                )
            )
    return sorted(due, key=lambda c: c.due_at)
```

(Keep the existing docstring and comments; add the S62 paragraph. Import `Collection` from `collections.abc`.)

Replace `due_transfer_checks`:

```python
async def due_transfer_checks(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    kc_ids: Collection[uuid.UUID] | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[TransferCheck]:
    """(docstring unchanged, plus:) Candidates come from the milestones — retention shown,
    transfer not — and are due ``retention_min_days`` after the latest judged answer
    (``last_seen_at``, which only a judged answer moves). Evidence is read for the soonest
    ``limit`` of them (``due_reviews_limit`` by default) to confirm a setting is still unpractised;
    the rest stay due and surface as the queue drains (S62)."""
    settings = get_settings()
    now_aware = now or datetime.now(UTC)
    now_naive = naive_utc(now_aware)
    min_days = settings.retention_min_days
    cap = limit if limit is not None else settings.due_reviews_limit
    candidates: list[tuple[datetime, LearnerKCState]] = []
    for state in await _marked_states(session, learner_id, kc_ids, now=now_aware):
        if (
            state.retention_shown_at is None
            or state.setting_transfer_at is not None
            or state.last_seen_at is None
        ):
            continue
        when = naive_utc(state.last_seen_at) + timedelta(days=min_days)
        if when <= now_naive:
            candidates.append((when, state))
    candidates.sort(key=lambda c: c[0])
    candidates = candidates[:cap]
    evidence = await kc_evidence(session, learner_id, [s.kc_id for _, s in candidates])
    due: list[TransferCheck] = []
    for when, state in candidates:
        found = evidence.get(state.kc_id)
        if (
            found is None
            or found.transfer_shown
            or transfer.next_setting(found.practised_settings) is None
        ):
            continue
        due.append(
            TransferCheck(
                kc_id=state.kc_id,
                due_at=when.replace(tzinfo=UTC),
                ability=state.ability,
                uncertainty=state.uncertainty,
            )
        )
    return due
```

Check before relying on it: that `last_seen_at` is set only on the judged path of `record_observation` (it is: the self-report branch skips it, and the admin branch writes no state). If `seed_transfer` or `revoke_transfer` also writes `last_seen_at`, note it in the ledger and compare against the old `max(observed_at)` query in a test.

- [ ] **Step 6: Run the new tests and every check's existing tests**

Run: `uv run pytest tests/test_evidence_milestones.py tests/test_retention_checks.py tests/test_transfer.py tests/test_mastery.py -q`
Expected: PASS. An existing test that inserts `LearningEvent` rows directly (not through `record_observation`) after a due read may see a stale milestone: fix the test's arrangement to clear `evidence_marked_at` (the documented way), never the code — and ledger it.

- [ ] **Step 7: Full suite, db-check, commit**

Run: `uv run poe db-check && uv run poe check` (background; read the tail)
Expected: green.

```bash
git add app/models/learning.py app/learning/mastery.py db/migrations/versions/0077_s62_evidence_milestones.py tests/test_evidence_milestones.py
git commit -m "perf(mastery): retention and transfer checks start from milestones, not every event [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: Plan revision asks only about its subject

**Files:**
- Modify: `app/services/lesson_plan.py:243-273` (`_due_review_kc_ids`)
- Test: `tests/test_evidence_milestones.py` (append)

**Interfaces:**
- Consumes: `due_retention_checks(..., kc_ids=...)`, `due_transfer_checks(..., kc_ids=...)` (Task 3).

- [ ] **Step 1: Write the failing test**

```python
async def test_plan_revision_asks_only_about_its_subject(db_session, monkeypatch) -> None:
    from app.services import lesson_plan as plan_svc

    asked: list[object] = []
    real_r, real_t = mastery.due_retention_checks, mastery.due_transfer_checks

    async def spy_r(session, learner_id, **kw):
        asked.append(kw.get("kc_ids"))
        return await real_r(session, learner_id, **kw)

    async def spy_t(session, learner_id, **kw):
        asked.append(kw.get("kc_ids"))
        return await real_t(session, learner_id, **kw)

    monkeypatch.setattr(mastery, "due_retention_checks", spy_r)
    monkeypatch.setattr(mastery, "due_transfer_checks", spy_t)
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    subject_kcs = {kc.id}

    await plan_svc._due_review_kc_ids(db_session, learner.id, subject_kcs)

    assert asked == [subject_kcs, subject_kcs]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_evidence_milestones.py::test_plan_revision_asks_only_about_its_subject -v`
Expected: FAIL — `asked == [None, None]`.

- [ ] **Step 3: Implement**

In `_due_review_kc_ids`:

```python
    # Only this subject's components (S62): a learner with fifteen subjects revised one plan by
    # reading the evidence for all of them and throwing fourteen-fifteenths away.
    retention = await mastery.due_retention_checks(
        session, learner_id, kc_ids=subject_kc_ids, now=now
    )
    transfer = await mastery.due_transfer_checks(
        session, learner_id, kc_ids=subject_kc_ids, now=now
    )
```

Keep the existing `if kc_id not in subject_kc_ids` filter: `mastery.due_reviews` (FSRS) is still global.

- [ ] **Step 4: Run plan tests**

Run: `uv run pytest tests/test_evidence_milestones.py tests/test_lesson_plan.py tests/test_retention_checks.py tests/test_transfer.py -q`
Expected: PASS (if `tests/test_lesson_plan.py` is named differently, run the files `grep -l revise_plan tests/` lists).

- [ ] **Step 5: Commit**

```bash
git add app/services/lesson_plan.py tests/test_evidence_milestones.py
git commit -m "perf(plans): revising a plan reads checks for its own subject only [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: Activity aggregates by day in SQL

**Files:**
- Modify: `app/services/analytics.py:171-216`
- Modify: `tests/test_history_budgets.py` (`PART_B`: remove `activity`)
- Test: `tests/test_history_budgets.py`, `tests/test_analytics.py`

- [ ] **Step 1: Remove the expected-failure marker (RED)**

Delete the `"activity": …` entry from `PART_B`.

Run: `uv run pytest "tests/test_history_budgets.py::test_a_hot_path_does_not_grow_with_history[activity]" -v`
Expected: FAIL — rows grew.

- [ ] **Step 2: Implement**

Replace the body's query and Python loop:

```python
    last_7d_start = naive_now - timedelta(days=7)
    prior_7d_start = naive_now - timedelta(days=14)
    # One answer fans out into a row per tagged KC. Count the attempt, not the evidence, or
    # momentum would reward broad KC tagging over learner effort. Rows written before
    # attempt_id existed have none and each stand alone. An attempt's rows share one
    # transaction's clock, so counting distinct attempts per day and summing is exact.
    #
    # Aggregated here rather than in Python (S62): one row per day of the 90-day window,
    # not one per event in it.
    attempt = func.coalesce(LearningEvent.attempt_id, LearningEvent.id)
    day = func.date_trunc("day", LearningEvent.created_at)
    rows = (
        await session.execute(
            select(
                day,
                func.count(distinct(attempt)).filter(LearningEvent.created_at >= last_7d_start),
                func.count(distinct(attempt)).filter(
                    LearningEvent.created_at >= prior_7d_start,
                    LearningEvent.created_at < last_7d_start,
                ),
            )
            .where(
                LearningEvent.learner_id == learner_id,
                # Both kinds: streak and momentum measure effort, not evidence.
                LearningEvent.event_type.in_(mastery.ATTEMPT_EVENTS),
                LearningEvent.created_at >= lookback_start,
            )
            .group_by(day)
        )
    ).all()
    active_days = {d.date() for d, _, _ in rows}
    observations_last_7d = sum(int(n or 0) for _, n, _ in rows)
    observations_prior_7d = sum(int(n or 0) for _, _, n in rows)
```

(import `distinct` from `sqlalchemy` if it is not already imported; the `ActivityRead(...)` return is unchanged.)

- [ ] **Step 3: Run activity's tests and the budget**

Run: `uv run pytest tests/test_analytics.py "tests/test_history_budgets.py::test_a_hot_path_does_not_grow_with_history[activity]" -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/services/analytics.py tests/test_history_budgets.py
git commit -m "perf(analytics): activity counts attempts per day in SQL, not per event in Python [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 6: Profile refresh's budget measures a filled window

**Files:**
- Modify: `tests/test_history_budgets.py` (`small_bounds`; `PART_B`: remove `profile_refresh`)

The measured cause (above) is the test, not the code: `profile_event_window` (2000) and `profile_message_window` (500) are larger than the small history (500 events, 200 messages), so only the larger history fills them. The windows are already count-bounded.

- [ ] **Step 1: Remove the marker (RED)**

Delete the `"profile_refresh": …` entry from `PART_B` (leaving `PART_B: dict[str, str] = {}` with its docstring).

Run: `uv run pytest "tests/test_history_budgets.py::test_a_hot_path_does_not_grow_with_history[profile_refresh]" -v`
Expected: FAIL — rows grew.

- [ ] **Step 2: Shrink the windows below the small history**

In `small_bounds`:

```python
    monkeypatch.setattr(settings, "profile_event_window", 100)
    monkeypatch.setattr(settings, "profile_message_window", 50)
```

- [ ] **Step 3: Run the whole budget file**

Run: `uv run pytest tests/test_history_budgets.py -q`
Expected: 13 passed, 0 xfailed. If `profile_refresh` still grows, it is a real cause: read its statements in the failure output, ledger the finding, and fix it test-first before continuing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_history_budgets.py
git commit -m "test(perf): the profile budget compares filled windows, so no path is expected to grow [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 7: Rerun the report, record it, close S62

**Files:**
- Modify: `docs/superpowers/specs/2026-10-09-long-history-performance-design.md` ("Results": a "Part B rerun" subsection)
- Modify: `docs/RUNBOOK.md` §20, `docs/guru-suggestions-tracker.md` (S62 row), `CLAUDE.md` (S62 clause)

- [ ] **Step 1: Full gate**

Run: `uv run poe check` (background) and `uv run poe format-check`; `uv run poe db-check`.
Expected: green; xfailed count drops by 2 (the two PART_B entries are gone).

- [ ] **Step 2: Rerun the report**

Run: `uv run poe perf-report --json /tmp/s62-part-b.json > /tmp/s62-part-b.md` (background; reuses seed v2; migrates `guru_perf` to 0077 first; the first timed path heals the milestones in its warm-ups).
Expected: every path under 250 ms p95. If one is not, profile it (statement grouping as in "Measured causes") and stop to report: that is a new finding for the user, not a ruling.

- [ ] **Step 3: Record**

- Spec "Results": add "### Part B rerun (2026-10-10)" with the new table and one line per fix → path.
- RUNBOOK §20: the milestones (`unaided_last_at`, `retention_shown_at`, `setting_transfer_at`, `evidence_marked_at`) and how to recompute them: `UPDATE learner_kc_state SET evidence_marked_at = NULL [WHERE learner_id = …]` after changing `retention_min_days` or the transfer catalogue; the next due read refills them.
- Tracker: S62 to Completed (rows table, counts, "Next up"), its deferred minors kept.
- CLAUDE.md: extend the S43/S62-adjacent text with one clause: "retention and transfer checks start from one-way milestones on `learner_kc_state` and read evidence only for candidates (S62)".

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-10-09-long-history-performance-design.md docs/RUNBOOK.md docs/guru-suggestions-tracker.md CLAUDE.md
git commit -m "docs: S62 part B results — the rerun selects nothing [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```
