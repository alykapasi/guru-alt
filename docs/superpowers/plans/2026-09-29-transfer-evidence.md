# Transfer Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Questions carry a setting from a fixed catalogue; a component shows transfer with an unaided, judged, correct answer in a setting none of its earlier attempts used; after retention, a component without transfer gets a cold transfer check in its review queue, generated in the next unpractised setting; analytics and the dashboard show it.

**Architecture:** `app/learning/transfer.py` owns the catalogue. Migration 0073 adds `items.setting` (NULL = abstract). `kc_evidence` gains `practised_settings` / `transfer_setting` from one extra ordered query; `mastery.due_transfer_checks` mirrors `due_retention_checks`. Piece 3's queue plumbing (`DueReviews`, `revise_steps` flags, `_with_retention_checks`, cold start) is generalised to a third kind.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic, pytest; React + vitest.

**Spec:** `docs/superpowers/specs/2026-09-29-transfer-evidence-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check` and `uv run poe format-check`. After the migration and model change: `uv run python -m tests.testdb`, `uv run poe db-upgrade`, `uv run poe db-check` → "No new upgrade operations detected." Map `sa.Text()` as `mapped_column(Text, ...)`. Frontend tasks: `npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint`; `uv run poe api-types` then `uv run poe api-contract`.
- Alembic revision `0073_item_settings`, `down_revision = "0072_grading_snapshots"`.
- One tracker id per commit subject: `[S14]`. Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls. Due dates served aware UTC (piece 3's fix).

## Deviations from the spec's wording (decided here)

- **Transfer needs an earlier attempt.** The spec's rule — "a setting that did not occur in any earlier attempt" — is trivially true of a component's *first* answer, which would then "show transfer" on nothing. The rule here also requires at least one earlier attempt at the component. That is the spec's intent ("after learning it in …"); cost if wrong: none — a first answer was never a claim about applying something learned.
- `ItemCreate.setting` is validated against the catalogue (an unknown name is a 422), since `ItemCreate` is also a request schema.

## Review Focus

1. A component's first-ever answer, unaided and correct: not transfer (Task 2 test).
2. An item deleted after it was answered: its attempts count as abstract, not as missing (Task 2 test).
3. A component due both a retention and a transfer check: one entry, a retention check (Task 3 test).
4. Every catalogue setting practised: never due (Task 2 test).
5. The shaped fake provider still recognises every generator's prompt with a setting line appended (Task 1 test).

---

### Task 1: Settings on questions [S14]

**Files:**
- Create: `app/learning/transfer.py`, `db/migrations/versions/0073_item_settings.py`
- Modify: `app/models/assessment.py` (`Item.setting`), `app/schemas/assessment.py` (`ItemCreate.setting`), `app/services/assessment.py` (`create_item` stores it), `app/learning/item_generation.py` (every generator and `GeneratorFn` take `setting`), `tests/test_migrations_with_data.py`
- Test: `tests/test_transfer.py` (create), `tests/test_shaped_provider.py`

**Interfaces:**
- Produces:
  ```python
  # app/learning/transfer.py
  ABSTRACT: str = "abstract"
  SETTINGS: tuple[tuple[str, str], ...]     # (name, gloss), fixed order, ABSTRACT first
  NAMES: frozenset[str]
  def setting_of(value: str | None) -> str  # None -> ABSTRACT
  def next_setting(practised: Iterable[str]) -> str | None
  def prompt_line(setting: str | None) -> str  # "" or " Set the question in this setting: …"
  # generators: setting: str | None = None (keyword); Item.setting: str | None
  ```

- [ ] **Step 1: Failing tests** — `tests/test_transfer.py`:

```python
"""Transfer: an unaided answer in a setting the component was never practised in (S14)."""

import json
import uuid

import pytest

from app.learning import item_generation, transfer
from app.llm.registry import fake_llm_client
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner

SHORT_REPLY = json.dumps({"stem": "A shop sells...", "criteria": ["a", "b"]})


def test_the_catalogue_starts_abstract_and_has_no_duplicates() -> None:
    names = [name for name, _ in transfer.SETTINGS]
    assert names[0] == transfer.ABSTRACT and len(names) == len(set(names)) == 12


def test_no_setting_is_abstract() -> None:
    assert transfer.setting_of(None) == transfer.ABSTRACT
    assert transfer.setting_of("money") == "money"


def test_the_next_setting_skips_abstract_and_what_was_practised() -> None:
    assert transfer.next_setting({"abstract"}) == "everyday"
    assert transfer.next_setting({"abstract", "everyday"}) == "money"
    assert transfer.next_setting(transfer.NAMES) is None


async def _kc(session) -> tuple[Learner, KC]:
    learner = Learner(handle=f"tr-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:6]}", name="Percentages")
    session.add(kc)
    await session.flush()
    return learner, kc


async def test_a_setting_reaches_the_prompt_and_the_item(db_session) -> None:
    learner, kc = await _kc(db_session)
    llm = fake_llm_client(SHORT_REPLY)
    item, _ = await item_generation.generate_short_item(
        db_session, llm, kc, owner_learner_id=learner.id, setting="money"
    )
    assert item is not None and item.setting == "money"
    provider = llm._providers["fake"]  # check the attribute name on LLMClient
    [(system, _messages)] = provider.prompts_sent
    assert "Set the question in this setting: money" in system


async def test_no_setting_leaves_prompt_and_item_as_before(db_session) -> None:
    learner, kc = await _kc(db_session)
    llm = fake_llm_client(SHORT_REPLY)
    item, _ = await item_generation.generate_short_item(
        db_session, llm, kc, owner_learner_id=learner.id
    )
    assert item is not None and item.setting is None
```

(Find how a test reaches the `FakeProvider` inside an `LLMClient` — or build the client from a `FakeProvider` you hold, as `tests/test_check_criteria.py::_client` does — and use that.)

Append to `tests/test_shaped_provider.py`:

```python
@pytest.mark.parametrize(("expected", "prompt"), REAL_PROMPTS, ids=[n for n, _ in REAL_PROMPTS])
def test_a_setting_line_does_not_change_which_shape_answers(expected: str, prompt: str) -> None:
    """Review focus 5: generators append a setting sentence; the marker must still match."""
    withline = prompt + transfer.prompt_line("money")
    assert [shape.name for shape in SHAPES if shape.marker in withline] == [expected]
```

Append to `tests/test_migrations_with_data.py`:

```python
async def test_existing_items_have_no_setting() -> None:
    """0073 (S14): a nullable column; NULL is read as abstract."""
    async with database_at("0072_grading_snapshots") as connect:
        conn = await connect()
        try:
            item_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO items (id, item_type, stem, difficulty, visibility, origin) "
                "VALUES ($1, 'short', 'Q', 0.0, 'private', 'generated')",
                item_id,
            )
        finally:
            await conn.close()
        await upgrade(SCRATCH, "0073_item_settings")
        conn = await connect()
        try:
            assert await conn.fetchval("SELECT setting FROM items WHERE id = $1", item_id) is None
        finally:
            await conn.close()
```

(Match the `items` insert to whatever NOT NULL columns 0072 has — read an earlier test in the file that inserts an item, e.g. for 0052, and copy its column list.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_transfer.py tests/test_shaped_provider.py -q` → FAIL (`app.learning.transfer` missing).

- [ ] **Step 3: Implement.**

`app/learning/transfer.py`:

```python
"""Transfer: applying a component in a setting it was never practised in (S14).

Two different item ids for one component were never "genuinely different applications" — one
template, one generator — so the old claim was removed (goal-policy design §4.3). It returns
with a difference the system can point at: a *named setting* from a fixed catalogue, recorded
on the question, compared exactly. A question with no setting is abstract — which is what
nearly every question written before this was.
"""

from collections.abc import Iterable

ABSTRACT = "abstract"

SETTINGS: tuple[tuple[str, str], ...] = (
    (ABSTRACT, "no real-world setting; the idea on its own terms"),
    ("everyday", "household life, routines and shopping"),
    ("money", "prices, budgets, interest or trade"),
    ("physics", "motion, forces, energy or light"),
    ("biology", "living things, bodies and cells"),
    ("engineering", "building, machines and design"),
    ("computing", "software, data and networks"),
    ("sport", "games, training and competition"),
    ("health", "medicine, nutrition and fitness"),
    ("society", "people, history and government"),
    ("arts", "music, art and writing"),
    ("nature", "weather, geography and ecology"),
)
"""The order is the order checks move through: a failed check in one setting leaves it
practised, so the next check takes the next one."""

NAMES: frozenset[str] = frozenset(name for name, _ in SETTINGS)
_GLOSS = dict(SETTINGS)


def setting_of(value: str | None) -> str:
    return value or ABSTRACT


def next_setting(practised: Iterable[str]) -> str | None:
    """The first catalogue setting a transfer check could use, or None when all are practised."""
    seen = set(practised)
    return next((name for name, _ in SETTINGS if name != ABSTRACT and name not in seen), None)


def prompt_line(setting: str | None) -> str:
    """The sentence a generator adds for ``setting``; nothing for none."""
    if setting is None:
        return ""
    return f" Set the question in this setting: {setting} ({_GLOSS[setting]})."
```

Migration `db/migrations/versions/0073_item_settings.py` (docstring: "Item settings (S14): the named setting a question is set in, for transfer. NULL is abstract."): `op.add_column("items", sa.Column("setting", sa.Text(), nullable=True))`; downgrade drops it. `revision = "0073_item_settings"`, `down_revision = "0072_grading_snapshots"`.

`Item.setting: Mapped[str | None] = mapped_column(Text, default=None)` with a comment ("The named setting it is set in (S14, ``app.learning.transfer``); None is abstract.") — import `Text` if absent.

`ItemCreate.setting: str | None = None` with a validator rejecting names outside `transfer.NAMES`; `create_item` passes `setting=data.setting` into `Item(...)` (read `create_item` and add it beside `difficulty`).

`item_generation`: every generator gains `setting: str | None = None` after `target_difficulty`; the system prompt becomes `<PROMPT> + _pitch(target_difficulty) + transfer.prompt_line(setting)`; `ItemCreate(..., setting=setting)`. `GeneratorFn.__call__` gains `setting: str | None = None`.

- [ ] **Step 4: Run** — `uv run python -m tests.testdb`; `uv run pytest tests/test_transfer.py tests/test_shaped_provider.py tests/test_migrations_with_data.py tests/test_item_generation*.py -q` → PASS; `uv run poe db-upgrade && uv run poe db-check` → no new operations; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/transfer.py db/migrations/versions/0073_item_settings.py app/models/assessment.py app/schemas/assessment.py app/services/assessment.py app/learning/item_generation.py tests/test_transfer.py tests/test_shaped_provider.py tests/test_migrations_with_data.py
git status
git commit -m "feat(transfer): questions carry a setting from a fixed catalogue [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: What counts as transfer, and when a check is due [S14]

**Files:**
- Modify: `app/learning/mastery.py` (`KCEvidence.practised_settings`, `transfer_setting`, `transfer_shown`; `kc_evidence`; `TransferCheck`; `due_transfer_checks`)
- Test: `tests/test_transfer.py` (append)

**Interfaces:**
- Consumes: `transfer.setting_of`, `transfer.next_setting` (Task 1); piece 3's `naive_utc`, `RetentionCheck`.
- Produces:
  ```python
  KCEvidence.practised_settings: frozenset[str] = frozenset()
  KCEvidence.transfer_setting: str | None = None
  KCEvidence.transfer_shown -> bool          # property
  TransferCheck = RetentionCheck-shaped model (kc_id, due_at aware UTC, ability, uncertainty)
  async def due_transfer_checks(session, learner_id, *, now=None) -> list[TransferCheck]
  ```

- [ ] **Step 1: Failing tests** (append; reuse `tests.test_item_exposure` fixtures as `tests/test_retention_checks.py` does, but create items with a `setting`):

```python
from datetime import timedelta

from app.learning import mastery
from app.learning.mastery import Observation
from app.models.assessment import Item, ItemKC, ItemType
from tests.test_item_exposure import T0, _kc as _exposure_kc, _learner


async def _item_in(session, kc, setting: str | None) -> Item:
    item = Item(visibility="curated", item_type=ItemType.MCQ, stem=f"q-{uuid.uuid4().hex[:6]}",
                difficulty=0.0, answer_key={"correct": 0}, setting=setting)
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id, weight=1.0))
    await session.flush()
    return item


async def _answer(session, learner, kc, item, *, when, correct=True, hints=None,
                  taught_first=False) -> None:
    await mastery.record_observation(
        session,
        Observation(learner_id=learner.id, kc_weights={kc.id: 1.0},
                    score=1.0 if correct else 0.0, correct=correct, item_id=item.id,
                    hints_used=hints, taught_first=taught_first),
        now=when,
    )
    await session.flush()


async def _evidence(session, learner, kc) -> mastery.KCEvidence:
    (ev,) = (await mastery.kc_evidence(session, learner.id, [kc.id])).values()
    return ev


async def test_a_correct_unaided_answer_in_a_new_setting_is_transfer(db_session) -> None:
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, None), when=T0)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, "money"),
                  when=T0 + timedelta(days=3))
    ev = await _evidence(db_session, learner, kc)
    assert ev.transfer_shown and ev.transfer_setting == "money"
    assert ev.practised_settings == frozenset({"abstract", "money"})


async def test_a_first_answer_is_not_transfer(db_session) -> None:
    """Review focus 1."""
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, "money"), when=T0)
    assert not (await _evidence(db_session, learner, kc)).transfer_shown


@pytest.mark.parametrize(
    "variant", ["practised", "wrong", "hinted", "taught_first"]
)
async def test_what_is_not_transfer(db_session, variant: str) -> None:
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    first = "money" if variant == "practised" else None
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, first), when=T0)
    await _answer(
        db_session, learner, kc, await _item_in(db_session, kc, "money"),
        when=T0 + timedelta(days=3),
        correct=variant != "wrong",
        hints=1 if variant == "hinted" else None,
        taught_first=variant == "taught_first",
    )
    assert not (await _evidence(db_session, learner, kc)).transfer_shown


async def test_a_deleted_item_counts_as_abstract(db_session) -> None:
    """Review focus 2."""
    from sqlalchemy import delete

    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    old = await _item_in(db_session, kc, None)
    await _answer(db_session, learner, kc, old, when=T0)
    await db_session.execute(delete(Item).where(Item.id == old.id))
    await db_session.flush()
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, "money"),
                  when=T0 + timedelta(days=3))
    ev = await _evidence(db_session, learner, kc)
    assert ev.transfer_setting == "money" and "abstract" in ev.practised_settings


async def _retained(session):
    """Retention shown: two unaided answers, abstract, a week apart."""
    learner = await _learner(session)
    _s, kc = await _exposure_kc(session)
    await _answer(session, learner, kc, await _item_in(session, kc, None), when=T0)
    await _answer(session, learner, kc, await _item_in(session, kc, None),
                  when=T0 + timedelta(days=7))
    return learner, kc


async def _due(session, learner, now):
    return [c.kc_id for c in await mastery.due_transfer_checks(session, learner.id, now=now)]


async def test_a_transfer_check_is_due_after_retention(db_session) -> None:
    learner, kc = await _retained(db_session)
    assert await _due(db_session, learner, T0 + timedelta(days=7, hours=2)) == []
    [check] = await mastery.due_transfer_checks(
        db_session, learner.id, now=T0 + timedelta(days=9)
    )
    assert check.kc_id == kc.id and check.due_at.utcoffset() == timedelta(0)


async def test_no_check_before_retention_or_after_transfer(db_session) -> None:
    learner = await _learner(db_session)
    _s, kc = await _exposure_kc(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, None), when=T0)
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []

    learner, kc = await _retained(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, "money"),
                  when=T0 + timedelta(days=9))
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_no_check_when_every_setting_is_practised(db_session) -> None:
    """Review focus 4."""
    learner, kc = await _retained(db_session)
    for i, name in enumerate(n for n in transfer.NAMES if n != transfer.ABSTRACT):
        await _answer(db_session, learner, kc, await _item_in(db_session, kc, name),
                      when=T0 + timedelta(days=8, minutes=i), correct=False)
    assert await _due(db_session, learner, T0 + timedelta(days=30)) == []


async def test_a_failed_check_moves_to_the_next_setting(db_session) -> None:
    learner, kc = await _retained(db_session)
    await _answer(db_session, learner, kc, await _item_in(db_session, kc, "everyday"),
                  when=T0 + timedelta(days=9), correct=False)
    ev = await _evidence(db_session, learner, kc)
    assert transfer.next_setting(ev.practised_settings) == "money"
    assert await _due(db_session, learner, T0 + timedelta(days=9, hours=12)) == []
    assert await _due(db_session, learner, T0 + timedelta(days=10, hours=1)) == [kc.id]
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_transfer.py -q` → the new tests FAIL.

- [ ] **Step 3: Implement** in `app/learning/mastery.py`:

- `KCEvidence` gains:
  ```python
      # Every setting this component's attempts were set in (S14; NULL and deleted items are
      # abstract), and the setting of the earliest unaided, judged, correct answer in a setting
      # no earlier attempt used — transfer. None until then. A first answer is never transfer:
      # there is nothing it was applied *beyond*.
      practised_settings: frozenset[str] = frozenset()
      transfer_setting: str | None = None

      @property
      def transfer_shown(self) -> bool:
          return self.transfer_setting is not None
  ```
- `kc_evidence`, after building `out`, runs one more query for the same learner and KCs:
  `select(LearningEvent.kc_id, <when>, <demonstrated>, <unassisted>, LearningEvent.payload["correct"].astext, Item.setting)` from `LearningEvent` **left outer join** `Item` on `Item.id == cast(LearningEvent.payload["item_id"].astext, Uuid)` (use the existing `_observed_when()`, `_demonstrated_clause()`, `_unassisted_clause()` as selected boolean columns), `where` event type in `ATTEMPT_EVENTS`, ordered by kc_id then when. Walk rows per KC keeping `practised: set[str]`; for each row: `setting = transfer.setting_of(row.setting)`; if `transfer_setting is None and practised and demonstrated and unassisted and correct == "true" and setting not in practised` → `transfer_setting = setting`; then `practised.add(setting)`. Write both into the KC's `KCEvidence` (`model_copy(update=...)`). A KC whose events have no `item_id` contributes abstract.
- `TransferCheck` — the same fields as `RetentionCheck` (subclass it or alias it; keep one shape).
- `due_transfer_checks(session, learner_id, *, now=None)`:
  ```python
      settings = get_settings()
      now_naive = naive_utc(now or datetime.now(UTC))
      min_days = settings.retention_min_days
      states = ...all LearnerKCState for the learner...
      evidence = await kc_evidence(session, learner_id, [s.kc_id for s in states])
      latest = await _latest_judged_at(session, learner_id, [s.kc_id for s in states])
      for state in states:
          found = evidence.get(state.kc_id)
          if (found is None or not found.retention_shown(min_days=min_days)
                  or found.transfer_shown
                  or transfer.next_setting(found.practised_settings) is None
                  or state.kc_id not in latest):
              continue
          when = naive_utc(latest[state.kc_id]) + timedelta(days=min_days)
          if when <= now_naive:
              due.append(TransferCheck(kc_id=..., due_at=when.replace(tzinfo=UTC), ...))
      return sorted(due, key=lambda c: c.due_at)
  ```
  with `_latest_judged_at` = `max(_observed_when())` grouped by kc over `_demonstrated_clause()` events for the learner and KCs. Docstring states the four conditions and that it is derived, never stored.

- [ ] **Step 4: Run** — `uv run pytest tests/test_transfer.py tests/test_retention_checks.py tests/test_item_exposure.py tests/test_evidence_kinds.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/mastery.py tests/test_transfer.py
git status
git commit -m "feat(transfer): an unaided answer in an unpractised setting shows transfer [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Transfer checks in the queue [S14]

**Files:**
- Modify: `app/learning/lesson_plan.py` (`StepDict.transfer_check`, `revise_steps(transfer_check_kc_ids=)`), `app/services/lesson_plan.py` (`DueReviews.transfer_checks`, `_due_review_kc_ids`, callers, `PlanGroundingContext.transfer_check`), `app/services/workflow.py` (cold; item), `app/services/session_runner.py` (`transfer_item_for_kc`, `next_item`, `due_review_items` merge, `_effective_item_type`), `app/learning/mastery.py` (`ReviewItem.kind` gains the literal), `app/schemas/assessment.py` (`ReviewItemRead.kind`)
- Test: `tests/test_transfer.py` (append), `tests/test_workflow.py` (append)

**Interfaces:**
- Consumes: `due_transfer_checks`, `KCEvidence.practised_settings` (Task 2); `transfer.next_setting` (Task 1).
- Produces: `DueReviews(kc_ids, retention_checks, transfer_checks)`; `StepDict.transfer_check`; `PlanGroundingContext.transfer_check: bool = False`; `session_runner.transfer_item_for_kc(session, llm, *, learner_id, kc) -> Item | None`; `kind: Literal["review", "retention_check", "transfer_check"]`.

- [ ] **Step 1: Failing tests** (append to `tests/test_transfer.py`):

```python
from app.learning import lesson_plan as engine
from app.services import lesson_plan as plan_svc
from app.services import session_runner


def test_a_due_transfer_check_is_a_flagged_review_step() -> None:
    kc = uuid.uuid4()
    steps = engine.revise_steps([], mastered_kc_ids=[], due_review_kc_ids=[kc],
                                transfer_check_kc_ids=[kc], scaffolding=engine.ScaffoldingHints())
    [step] = [s for s in steps if s["step_type"] == "review"]
    assert step["transfer_check"] is True and step["retention_check"] is False


async def test_retention_wins_over_transfer_for_one_component(db_session, monkeypatch) -> None:
    """Review focus 3."""
    learner, kc = await _retained(db_session)
    fake_check = mastery.RetentionCheck(kc_id=kc.id, due_at=T0, ability=0.0, uncertainty=1.0)

    async def both(*_a, **_k):
        return [fake_check]

    monkeypatch.setattr(mastery, "due_retention_checks", both)
    due = await plan_svc._due_review_kc_ids(db_session, learner.id, {kc.id},
                                            now=T0 + timedelta(days=9))
    assert due.kc_ids == [kc.id]
    assert due.retention_checks == frozenset({kc.id}) and due.transfer_checks == frozenset()


async def test_the_queue_serves_a_transfer_check_in_the_next_setting(db_session) -> None:
    learner, kc = await _retained(db_session)
    pairs = await session_runner.due_review_items(
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, item_limit=5,
        now=T0 + timedelta(days=9),
    )
    [(review, item)] = [(r, i) for r, i in pairs if r.kc_id == kc.id]
    assert review.kind == "transfer_check"
    assert item is not None and item.item_type == ItemType.SHORT and item.setting == "everyday"
```

Append to `tests/test_workflow.py` a `_mark_active_step_transfer_check` helper (as `_mark_active_step_retention_check`) and `test_a_transfer_check_is_posed_cold_in_a_new_setting`: flagged step → `captured[0] == CHECK_FIRST_SYSTEM_PROMPT`, `taught_first` False, and the item the workflow poses has a non-abstract `setting` (capture it via `assessment_svc.answer_item`'s `item` argument).

- [ ] **Step 2: Run** — `uv run pytest tests/test_transfer.py tests/test_workflow.py -q` → new tests FAIL.

- [ ] **Step 3: Implement.**

- `revise_steps(..., transfer_check_kc_ids: Iterable[uuid.UUID] = ())`; final loop sets `step["transfer_check"] = step["step_type"] == "review" and kc in transfer and not step["retention_check"]`. `StepDict.transfer_check: NotRequired[bool]` with a comment.
- `DueReviews` gains `transfer_checks: frozenset[uuid.UUID]`; `_due_review_kc_ids` merges `mastery.due_transfer_checks(...)` like retention checks (subject filter, earliest due), and `transfer_checks` excludes any KC already in `retention_checks`. Every `revise_steps` call passes `transfer_check_kc_ids=due_reviews.transfer_checks`.
- `PlanGroundingContext.transfer_check: bool = False`; `get_active_step_context` reads it.
- `session_runner.transfer_item_for_kc(session, llm, *, learner_id, kc)`:
  ```python
      evidence = (await mastery.kc_evidence(session, learner_id, [kc.id])).get(kc.id)
      setting = transfer.next_setting(evidence.practised_settings if evidence else ())
      if setting is None:
          return None
      # An unseen question already set there, else one written for it (S14).
      ...find_item_for_kc(..., item_type=SHORT, unseen_only=True) filtered to that setting —
      add a `setting: str | None = None` filter to assessment.find_item_for_kc...
      ...else _generate_owned(..., generator=generate_short_item, setting=setting) — thread
      `setting` through _generate_owned...
  ```
  Read `assessment.find_item_for_kc` and `_generate_owned` and add the `setting` parameter to each (default None = no filter / no setting).
- `workflow`: `cold = step.check_first or step.retention_check or step.transfer_check`; the item is `await transfer_item_for_kc(...)` when `step.transfer_check`, else `short_answer_item_for_kc(...)` as today.
- `session_runner.next_item`: when `context.transfer_check`, return `transfer_item_for_kc(...)`. `_effective_item_type`: transfer checks, like retention checks, are SHORT.
- `ReviewItem.kind` / `ReviewItemRead.kind`: add `"transfer_check"`. Generalise `_with_retention_checks(reviews, checks)` to `_with_checks(reviews, retention, transfer)`: transfer entries first, then retention entries overwrite (retention wins), each at the earliest date, aware UTC. The item for a `transfer_check` entry is `transfer_item_for_kc`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_transfer.py tests/test_retention_checks.py tests/test_workflow.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/lesson_plan.py app/services/lesson_plan.py app/services/workflow.py app/services/session_runner.py app/services/assessment.py app/learning/mastery.py app/schemas/assessment.py tests/test_transfer.py tests/test_workflow.py
git status
git commit -m "feat(transfer): a transfer check comes due after retention, in the next setting [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Shown back to the learner [S14]

**Files:**
- Modify: `app/schemas/analytics.py` (`KCMasteryRead.transfer_shown`, `transfer_setting`), `app/services/analytics.py`, `frontend/src/components/dashboard/MasteryEvidence.tsx`, `frontend/src/components/dashboard/ReviewsDueCard.tsx`, the regenerated `frontend/src/api/schema.d.ts`
- Test: `tests/test_analytics.py` (append), `frontend/src/components/dashboard/MasteryEvidence.test.tsx`, `frontend/src/components/dashboard/ReviewsDueCard.test.tsx`

- [ ] **Step 1: Failing tests.**
  - `tests/test_analytics.py`: a learner with retention and a correct unaided `money` answer → the subject mastery read's KC has `transfer_shown is True` and `transfer_setting == "money"`; a KC without → `False` / `None`. (Copy the existing retention_shown analytics test's setup.)
  - `MasteryEvidence.test.tsx`: `kc({ retention_shown: true, transfer_shown: true, transfer_setting: "money" })` → text `applied in money`; `kc({ retention_shown: true, transfer_shown: false })` → `not yet applied in a new setting`; `kc({ retention_shown: false })` → neither phrase.
  - `ReviewsDueCard.test.tsx`: a third row `kind: "transfer_check"` → "Transfer check" once.
- [ ] **Step 2: Run** — pytest on the analytics test and `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/dashboard` → FAIL.
- [ ] **Step 3: Implement.**
  - `KCMasteryRead`: `transfer_shown: bool = False`, `transfer_setting: str | None = None` (field comment: "a setting from ``app.learning.transfer`` in which the component was applied unaided after being learned elsewhere (S14)"); analytics fills them from `_ev(evidence, kc.id)`.
  - `uv run poe api-types`, stage the schema, `uv run poe api-contract`.
  - `MasteryEvidence`: after the retention span, when `kc.retention_shown`: `{" · "}` then `<span className={kc.transfer_shown ? "text-success" : undefined}>{kc.transfer_shown ? `applied in ${kc.transfer_setting}` : "not yet applied in a new setting"}</span>`. Extend the component's doc comment with one line on transfer.
  - `ReviewsDueCard`: a label map `{ retention_check: "Retention check", transfer_check: "Transfer check" }`; render the badge when the row's kind has a label. While here, wrap the name and badge in `<div className="flex min-w-0 items-center gap-2">` so a long name truncates (piece 3's deferred minor — ledger it as taken with this change).
- [ ] **Step 4: Run** — the tests above → PASS; `uv run poe check && uv run poe format-check`; in `frontend/`: `npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint` → green.
- [ ] **Step 5: Commit**

```bash
git add app/schemas/analytics.py app/services/analytics.py tests/test_analytics.py frontend/src/components/dashboard/MasteryEvidence.tsx frontend/src/components/dashboard/MasteryEvidence.test.tsx frontend/src/components/dashboard/ReviewsDueCard.tsx frontend/src/components/dashboard/ReviewsDueCard.test.tsx frontend/src/api/schema.d.ts
git status
git commit -m "feat(transfer): the dashboard says where a component was applied [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Docs [S14]

- [ ] **Step 1:** Tracker S14 → `Implemented`: transfer done (catalogue settings, the rule, transfer checks after retention, dashboard); the final review's deferred minors; remove the ReviewsDueCard badge minor if Task 4 took it.
- [ ] **Step 2:** Goal-policy design §4.3 (end) and §11 "Transfer" bullet: "Returned: [transfer evidence](2026-09-29-transfer-evidence-design.md)."
- [ ] **Step 3:** CLAUDE.md, the achievement bullet: "Transfer is an unaided correct answer in a catalogue setting (`app/learning/transfer.py`) no earlier attempt used, checked after retention; it is shown as evidence and not required for achievement."
- [ ] **Step 4: Commit**

```bash
git add docs/guru-suggestions-tracker.md docs/superpowers/specs/2026-09-22-goal-policy-design.md CLAUDE.md
git status
git commit -m "docs: record transfer evidence [S14]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
