# Grading Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every grade records what it was measured against — frozen item, rubric and grading-prompt snapshots plus grader and model — and `uv run poe regrade` can put past answers through a current or recorded grader and report agreement.

**Architecture:** Each grading path stamps a `GradingProvenance` on its `GradeResult`. `answer_item` (the one funnel for every grade) writes content-addressed, per-learner snapshots to a new `grading_snapshots` table and copies their hashes into a `grading` block on every event of the attempt (event schema v5). A read-only re-grade service selects graded attempts, rebuilds the grader's inputs from snapshots, and compares.

**Tech Stack:** Python 3.13, SQLAlchemy async, PostgreSQL (JSONB, `ON CONFLICT DO NOTHING`), Alembic, pytest, argparse.

**Spec:** `docs/superpowers/specs/2026-09-28-grading-provenance-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check` and `uv run poe format-check`. After the migration and model change: `uv run python -m tests.testdb`, `uv run poe db-upgrade`, `uv run poe db-check` → "No new upgrade operations detected." Map `sa.Text()` columns as `mapped_column(Text, ...)`.
- Alembic revision id `0072_grading_snapshots`, `down_revision = "0071_call_accounting"`.
- Timestamps naive UTC.
- One tracker id per commit subject: `[S56]`. Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls: tests use `FakeProvider` / `fake_llm_client`.
- A re-grade never changes a score, event or mastery state. The re-grade report carries ids and scores only, never learner text.

## Review Focus

1. An attempt retried under the same `attempt_id` (idempotent replay, or losing a concurrent race): no duplicate snapshots, no second `grading` block, and the replayed grade is unchanged (Task 2 test).
2. A multi-component item whose rubric belongs to one KC: the rubric snapshot records the criteria against that component only, and a re-grade hands the grader the same shape (Task 3 test).
3. A rubric grade on an empty response (no model call): `grader` is `rubric`, `prompt` and `model` null, and a re-grade reproduces 0 without calling the model (Task 3 test).
4. An administrator's sudo answer (`admin_observation`): its `grading` block and snapshots are written like any other (Task 2 test).
5. A re-grade refused by the spend guard mid-run is counted `failed`, and the run finishes (Task 3 test).

---

### Task 1: Each grader says what it is [S56]

**Files:**
- Modify: `app/learning/grading.py`, `app/learning/rubric_grading.py`, `app/services/decisions.py`
- Test: `tests/test_grading_provenance.py` (create)

**Interfaces:**
- Produces:
  ```python
  # app/learning/grading.py
  Grader = Literal["auto", "self", "rubric", "jev"]
  class GradingProvenance(BaseModel):
      grader: Grader
      system_prompt: str | None = None
      template_version: int | None = None
      model: str | None = None  # "provider:model"
  GradeResult.provenance: GradingProvenance | None = None
  # app/learning/rubric_grading.py
  TEMPLATE_VERSION: int = 1
  async def grade_open(client, *, stem, response, rubric, components=(), max_tokens=512, system: str | None = None)
  ```

- [ ] **Step 1: Failing tests** — `tests/test_grading_provenance.py`:

```python
"""A grade says what produced it (S56)."""

import json

import pytest

from app.learning import rubric_grading
from app.learning.grading import auto_grade, grade_flashcard
from app.learning.rubric_grading import GradedComponent, grade_open
from app.learning.turn_read import FULLY_CORRECT, ReadContext
from app.llm.decisions import FakeDecisionClient, YesNoAnswer
from app.llm.registry import fake_llm_client
from app.models.assessment import ItemType
from app.services import decisions
from tests.decision_support import runtime, using

GRADE = json.dumps({"score": 0.8, "rationale": "ok"})


def test_auto_and_self_graders_name_themselves() -> None:
    mcq = auto_grade(ItemType.MCQ, {"correct_index": 1}, {"choice_index": 1})
    assert mcq.provenance is not None and mcq.provenance.grader == "auto"
    card = grade_flashcard({"rating": 3})
    assert card.provenance is not None and card.provenance.grader == "self"


async def test_the_rubric_grader_records_its_prompt_and_model() -> None:
    result, _ = await grade_open(
        fake_llm_client(GRADE), stem="Q", response={"text": "a"}, rubric=None
    )
    p = result.provenance
    assert p is not None and p.grader == "rubric"
    assert p.system_prompt == rubric_grading._SYSTEM_PROMPT
    assert p.template_version == rubric_grading.TEMPLATE_VERSION
    assert p.model == "fake:fake-1"


async def test_the_component_prompt_is_the_one_recorded_for_a_multi_component_item() -> None:
    import uuid

    comps = [GradedComponent(kc_id=uuid.uuid4(), name=n) for n in ("A", "B")]
    reply = json.dumps({"components": [{"n": 1, "score": 1}, {"n": 2, "score": 0}], "score": 0.5})
    result, _ = await grade_open(
        fake_llm_client(reply), stem="Q", response={"text": "a"}, rubric=None, components=comps
    )
    assert result.provenance is not None
    assert result.provenance.system_prompt == rubric_grading._COMPONENT_SYSTEM_PROMPT


async def test_an_empty_answer_names_the_grader_but_no_prompt_or_model() -> None:
    result, _ = await grade_open(
        fake_llm_client(GRADE), stem="Q", response={"text": "  "}, rubric=None
    )
    assert result.provenance is not None
    assert (result.provenance.grader, result.provenance.system_prompt, result.provenance.model) == (
        "rubric",
        None,
        None,
    )


async def test_a_system_override_is_what_is_sent_and_recorded() -> None:
    llm = fake_llm_client(GRADE)
    result, _ = await grade_open(
        llm, stem="Q", response={"text": "a"}, rubric=None, system="OLD PROMPT"
    )
    assert result.provenance is not None and result.provenance.system_prompt == "OLD PROMPT"


async def test_a_live_jev_pass_names_jev(db_session) -> None:
    fake = FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.99)})

    async def smart():
        raise AssertionError("skipped")

    with using(runtime(fake, fully_correct="live")):
        result = await decisions.decide_grade(
            smart=smart,
            stem="q",
            answer="a",
            rubric_criteria=None,
            context=ReadContext(learner_id=None, conversation_id=None, item_id=None),
            attempt_id=None,
        )
    assert result.provenance is not None and result.provenance.grader == "jev"
```

(Check `auto_grade`'s MCQ key/response field names in `app/learning/grading.py::_grade_mcq` and adjust the two dicts if they differ; check `ReadContext`'s fields in `app/learning/turn_read.py` and the `NOBODY` constant in `tests/test_decisions.py`, and reuse it if importable.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_grading_provenance.py -q` → FAIL (`provenance` missing; `system` unexpected keyword).

- [ ] **Step 3: Implement.**

`app/learning/grading.py` — after the imports (add `from typing import Literal`):

```python
Grader = Literal["auto", "self", "rubric", "jev"]


class GradingProvenance(BaseModel):
    """What produced a grade (S56): which grader, and for the rubric model, the system prompt it
    was given, the version of the code that built its user message, and the model. Recorded so
    a past grade can be read — and re-graded — against exactly what measured it."""

    grader: Grader
    system_prompt: str | None = None
    template_version: int | None = None
    model: str | None = None
```

`GradeResult` gains, after `evidence_kind`:

```python
    provenance: GradingProvenance | None = None
    """Set by every grading path (S56). None only for a result rebuilt from the event log."""
```

`auto_grade` wraps its two returns: `result = _grade_mcq(...)` / `_grade_blanks(...)`, then
`return result.model_copy(update={"provenance": GradingProvenance(grader="auto")})`.
`grade_flashcard` passes `provenance=GradingProvenance(grader="self")`.

`app/learning/rubric_grading.py`:

```python
TEMPLATE_VERSION = 1
"""Version of ``_build_prompt`` — how the user message is assembled from question, rubric,
components and response. Bump it with any change there: it is recorded with every grade (S56),
and a re-grade under the recorded system prompt still rebuilds the message with today's code."""
```

`grade_open` gains `system: str | None = None` (last keyword). The empty-answer return becomes
`GradeResult(..., provenance=GradingProvenance(grader="rubric"))`. Before the call:

```python
    used_system = system or (_COMPONENT_SYSTEM_PROMPT if per_component else _SYSTEM_PROMPT)
    spec = client.spec(GRADING_ROLE)
```

pass `system=used_system`, and add to the returned `GradeResult`:

```python
        provenance=GradingProvenance(
            grader="rubric",
            system_prompt=used_system,
            template_version=TEMPLATE_VERSION,
            model=f"{spec.provider}:{spec.model}",
        ),
```

Import `GradingProvenance` beside `GradeResult`.

`app/services/decisions.py` — the live pass returns
`GradeResult(score=1.0, correct=True, detail={"method": "decision"}, provenance=GradingProvenance(grader="jev"))`
(import from `app.learning.grading`). Update `test_a_confident_live_pass_skips_the_smart_grader`
only if it compares the whole object.

- [ ] **Step 4: Run** — `uv run pytest tests/test_grading_provenance.py tests/test_rubric_grading.py tests/test_decisions.py tests/test_component_evidence.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/grading.py app/learning/rubric_grading.py app/services/decisions.py tests/test_grading_provenance.py
git status
git commit -m "feat(grading): every grade says which grader, prompt and model produced it [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Snapshots, and the `grading` block on every event [S56]

**Files:**
- Create: `db/migrations/versions/0072_grading_snapshots.py`, `app/models/grading.py`, `app/services/grading_history.py`
- Modify: `app/models/__init__.py`, `app/learning/mastery.py` (`Observation.grading`, three payloads, `EVENT_SCHEMA_VERSION = 5`), `app/services/assessment.py` (`answer_item`), `app/services/retention.py` (`RETENTION`, `export_learner`), `tests/test_migrations_with_data.py`
- Test: `tests/test_grading_provenance.py` (append)

**Interfaces:**
- Consumes: `GradingProvenance`, `GradeResult.provenance` (Task 1); `rubric_grading.GradedComponent`.
- Produces:
  ```python
  # app/models/grading.py
  class GradingSnapshot(Base): learner_id: uuid.UUID; sha256: str; kind: str; content: dict; created_at: datetime
  # app/services/grading_history.py
  def digest(content: dict) -> str
  async def keep(session, learner_id: uuid.UUID, kind: str, content: dict) -> str
  def item_content(item: Item, components: Sequence[GradedComponent]) -> dict
  def rubric_content(rubric: Rubric | None, components: Sequence[GradedComponent]) -> dict | None
  async def record(session, learner_id, item, components, result: GradeResult) -> dict  # the event's "grading" block
  ```
  `Observation.grading: dict | None`; every `observation`, `self_report` and `admin_observation` payload carries `"grading": obs.grading`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_grading_provenance.py`:

```python
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Item, ItemKC, Rubric
from app.models.grading import GradingSnapshot
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as svc
from app.services import retention


async def _setup(session: AsyncSession, *, item_type=ItemType.SHORT, rubric: dict | None = None):
    learner = Learner(handle=f"gp-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:6]}", name="Vectors")
    session.add(kc)
    await session.flush()
    rubric_row = None
    if rubric is not None:
        rubric_row = Rubric(owner_learner_id=learner.id, kc_id=kc.id, criteria=rubric)
        session.add(rubric_row)
        await session.flush()
    row = Item(
        owner_learner_id=learner.id,
        item_type=item_type,
        stem="Add two vectors.",
        answer_key={"correct_index": 1} if item_type == ItemType.MCQ else None,
        rubric_id=rubric_row.id if rubric_row else None,
    )
    session.add(row)
    await session.flush()
    session.add(ItemKC(item_id=row.id, kc_id=kc.id))
    await session.flush()
    item = await svc.get_item(session, row.id)
    assert item is not None
    return learner, kc, item


async def _events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.learner_id == learner_id)
            )
        ).all()
    )


async def test_a_rubric_grade_records_its_snapshots_on_the_event(db_session) -> None:
    learner, _kc, item = await _setup(db_session, rubric={"points": ["tip to tail"]})
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"text": "tip to tail"}),
        llm=fake_llm_client(GRADE),
    )
    [event] = await _events(db_session, learner.id)
    grading = event.payload["grading"]
    assert event.payload["schema_version"] == 5
    assert grading["grader"] == "rubric" and grading["model"] == "fake:fake-1"
    kinds = {
        s.sha256: s.kind
        for s in await db_session.scalars(
            select(GradingSnapshot).where(GradingSnapshot.learner_id == learner.id)
        )
    }
    assert kinds[grading["item"]] == "item"
    assert kinds[grading["rubric"]] == "rubric"
    assert kinds[grading["prompt"]] == "prompt"


async def test_an_auto_grade_has_an_item_snapshot_and_nothing_else(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice_index": 1}),
        llm=fake_llm_client(),
    )
    [event] = await _events(db_session, learner.id)
    g = event.payload["grading"]
    assert g["grader"] == "auto" and g["item"] and (g["rubric"], g["prompt"], g["model"]) == (
        None, None, None,
    )


async def test_the_same_item_graded_twice_is_one_snapshot(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    for _ in range(2):
        await svc.answer_item(
            db_session, learner.id, item, AnswerSubmit(response={"choice_index": 1}),
            llm=fake_llm_client(),
        )
    count = await db_session.scalar(
        select(func.count()).select_from(GradingSnapshot).where(
            GradingSnapshot.learner_id == learner.id
        )
    )
    assert count == 1


async def test_a_retried_attempt_writes_nothing_twice(db_session) -> None:
    """Review focus 1."""
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    attempt = uuid.uuid4()
    for _ in range(2):
        await svc.answer_item(
            db_session, learner.id, item,
            AnswerSubmit(response={"choice_index": 1}, attempt_id=attempt),
            llm=fake_llm_client(),
        )
    assert len(await _events(db_session, learner.id)) == 1


async def test_deleting_the_item_keeps_the_history(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice_index": 1}),
        llm=fake_llm_client(),
    )
    await db_session.execute(delete(Item).where(Item.id == item.id))
    await db_session.flush()
    [event] = await _events(db_session, learner.id)
    snapshot = await db_session.get(GradingSnapshot, (learner.id, event.payload["grading"]["item"]))
    assert snapshot is not None and snapshot.content["stem"] == "Add two vectors."


async def test_snapshots_are_exported_and_erased_with_the_account(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice_index": 1}),
        llm=fake_llm_client(),
    )
    exported = await retention.export_learner(db_session, learner.id)
    assert len(exported["grading_snapshots"]) == 1

    await db_session.execute(delete(Learner).where(Learner.id == learner.id))
    await db_session.flush()
    assert (await db_session.scalars(select(GradingSnapshot))).all() == []


async def test_an_administrators_answer_carries_provenance_too(db_session) -> None:
    """Review focus 4."""
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    admin = Learner(handle=f"adm-{uuid.uuid4().hex[:8]}", is_admin=True)
    db_session.add(admin)
    await db_session.flush()
    db_session.info["admin_actor_id"] = str(admin.id)
    try:
        await svc.answer_item(
            db_session, learner.id, item, AnswerSubmit(response={"choice_index": 1}),
            llm=fake_llm_client(),
        )
    finally:
        db_session.info.pop("admin_actor_id", None)
    [event] = await _events(db_session, learner.id)
    assert event.event_type == "admin_observation"
    assert event.payload["grading"]["grader"] == "auto"
```

Append to `tests/test_migrations_with_data.py` (match the 0071 test):

```python
async def test_grading_snapshots_table_arrives_empty() -> None:
    """0072 (S56): a new table; existing events are untouched and carry no grading block."""
    async with database_at("0071_call_accounting") as connect:
        await upgrade(SCRATCH, "0072_grading_snapshots")
        conn = await connect()
        try:
            assert await conn.fetchval("SELECT count(*) FROM grading_snapshots") == 0
        finally:
            await conn.close()
```

(`_setup`'s MCQ `answer_key` and the `{"choice_index": 1}` responses use the same field names as Task 1's test; if Task 1 found different names in `_grade_mcq`, use those here too. `svc.get_item` must load `rubric` and `kc_links`, as `_grade` already relies on.)

- [ ] **Step 2: Run** — `uv run pytest tests/test_grading_provenance.py tests/test_migrations_with_data.py -q` → FAIL (`app.models.grading` missing).

- [ ] **Step 3: Implement.**

`db/migrations/versions/0072_grading_snapshots.py`:

```python
"""Grading provenance (S56): frozen copies of what each grade was measured against.

Content-addressed and per learner: ``(learner_id, sha256)`` is the key, so an unchanged item
is stored once however often it is graded, and erasing the learner erases their copies. No
foreign key to items or rubrics — deleting either must not take a past grade's explanation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0072_grading_snapshots"
down_revision: str | Sequence[str] | None = "0071_call_accounting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grading_snapshots",
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sha256", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("grading_snapshots")
```

`app/models/grading.py`:

```python
"""Frozen copies of what a grade was measured against (S56)."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class GradingSnapshot(Base):
    """An item, rubric or grading prompt as it was when a grade used it.

    Keyed by the SHA-256 of its canonical JSON, per learner: written once, never updated, and
    deleted only with the learner. Events reference it by hash (``payload["grading"]``); there
    is no foreign key to ``items`` or ``rubrics`` on purpose, so deleting those keeps history.
    """

    __tablename__ = "grading_snapshots"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), primary_key=True
    )
    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # item | rubric | prompt
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

Register it in `app/models/__init__.py` (import and `__all__`).

`app/services/grading_history.py`:

```python
"""What a grade was measured against, frozen and referenced from the event (S56).

``answer_item`` calls ``record`` once per graded attempt; the returned block goes on every
event of the attempt. Snapshots live on the grading session, so a grade and its provenance
commit or roll back together.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning.grading import GradeResult
from app.learning.rubric_grading import GradedComponent
from app.models.assessment import Item, Rubric
from app.models.grading import GradingSnapshot


def digest(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def keep(session: AsyncSession, learner_id: uuid.UUID, kind: str, content: dict) -> str:
    sha = digest(content)
    await session.execute(
        pg_insert(GradingSnapshot)
        .values(learner_id=learner_id, sha256=sha, kind=kind, content=content)
        .on_conflict_do_nothing(index_elements=["learner_id", "sha256"])
    )
    return sha


def item_content(item: Item, components: Sequence[GradedComponent]) -> dict:
    return {
        "item_type": item.item_type,
        "stem": item.stem,
        "answer_key": item.answer_key,
        "difficulty": item.difficulty,
        "components": [
            {"kc_id": str(c.kc_id), "name": c.name, "description": c.description}
            for c in components
        ],
    }


def rubric_content(rubric: Rubric | None, components: Sequence[GradedComponent]) -> dict | None:
    """The criteria as the grader saw them: the rubric's own, and against which component."""
    if rubric is None or not rubric.criteria:
        return None
    return {
        "criteria": rubric.criteria,
        "components": [
            {"kc_id": str(c.kc_id), "criteria": c.criteria} for c in components if c.criteria
        ],
    }


async def record(
    session: AsyncSession,
    learner_id: uuid.UUID,
    item: Item,
    components: Sequence[GradedComponent],
    result: GradeResult,
) -> dict:
    provenance = result.provenance
    grader = provenance.grader if provenance is not None else "auto"
    rubric = rubric_content(item.rubric, components)
    prompt_sha = None
    if provenance is not None and provenance.system_prompt is not None:
        prompt_sha = await keep(
            session,
            learner_id,
            "prompt",
            {
                "system": provenance.system_prompt,
                "template_version": provenance.template_version,
            },
        )
    return {
        "grader": grader,
        "item": await keep(session, learner_id, "item", item_content(item, components)),
        "rubric": await keep(session, learner_id, "rubric", rubric) if rubric else None,
        "prompt": prompt_sha,
        "model": provenance.model if provenance is not None else None,
        "app_version": get_settings().app_version,
    }
```

`app/learning/mastery.py`: `EVENT_SCHEMA_VERSION = 5`, docstring gains
`5 — adds ``grading``: which grader, and hashes of the item, rubric and prompt snapshots it used
    (S56, ``grading_snapshots``). Absent on earlier rows, which cannot be re-graded.`
`Observation` gains `grading: dict | None = None` (after `taught_first`, with a one-line comment),
and each of the three payloads (`admin_observation`, `self_report`, `observation`) adds
`"grading": obs.grading,`.

`app/services/assessment.py::answer_item` — after `result = await _grade(...)`:

```python
    # What this grade was measured against, frozen (S56). Written on this session, so it
    # commits with the observation or not at all.
    grading = await grading_history.record(
        session, learner_id, item, await _components_of(session, item), result
    )
```

and pass `grading=grading` to `Observation(...)`. Import `grading_history` from `app.services`.

`app/services/retention.py`: a `RETENTION` entry
`StoreRetention("grading_snapshots", "deleted", "Cascades from the learner. Frozen copies of the items, rubrics and prompts that graded their answers (S56).")`
beside `learning_events`, and in `export_learner`:
`"grading_snapshots": await rows(GradingSnapshot, GradingSnapshot.learner_id == learner_id),`.

- [ ] **Step 4: Run** — `uv run python -m tests.testdb`; `uv run pytest tests/test_grading_provenance.py tests/test_migrations_with_data.py tests/test_retention.py tests/eval/test_datasets_replay.py -q` → PASS; `uv run poe db-upgrade && uv run poe db-check` → no new operations; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/versions/0072_grading_snapshots.py app/models/grading.py app/models/__init__.py app/services/grading_history.py app/learning/mastery.py app/services/assessment.py app/services/retention.py tests/test_grading_provenance.py tests/test_migrations_with_data.py
git status
git commit -m "feat(grading): every graded event references frozen snapshots of what graded it [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `uv run poe regrade` [S56]

**Files:**
- Create: `app/services/regrade.py`, `app/workers/regrade.py`, `tests/test_regrade.py`
- Modify: `pyproject.toml` (poe task)

**Interfaces:**
- Consumes: `GradingSnapshot`, the `grading` block (Task 2); `grade_open(..., system=)` (Task 1).
- Produces:
  ```python
  @dataclass(frozen=True) class Candidate:
      event_id: uuid.UUID; learner_id: uuid.UUID; score: float; correct: bool
      component_scores: dict[str, float]; response: dict; grading: dict
      item: dict; rubric: dict | None; prompt: dict | None
  @dataclass(frozen=True) class Plan:
      candidates: list[Candidate]; not_regradable: int; by_grader: dict[str, int]
  @dataclass(frozen=True) class Comparison: event_id: uuid.UUID; recorded: float; regraded: float
  @dataclass(frozen=True) class Report:
      compared: int; failed: int; correct_agreement: float | None
      mean_abs_score_diff: float | None; component_mean_abs_diff: float | None
      largest: list[Comparison]
  async def plan(session, *, since: datetime, until: datetime, learner_id=None, subject_id=None, limit=200) -> Plan
  def estimate_cost(plan: Plan, llm: LLMClient) -> float | None
  async def run(llm: LLMClient, plan: Plan, *, prompt: Literal["current", "recorded"] = "current") -> Report
  def render(plan: Plan, report: Report | None, cost: float | None) -> str
  ```

- [ ] **Step 1: Failing tests** — `tests/test_regrade.py`:

```python
"""Re-grading measures a grader against past answers and changes nothing (S56)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import rubric_grading
from app.llm import LLMClient
from app.llm.meter import BudgetExceeded
from app.llm.providers.fake import FakeProvider
from app.llm.registry import ModelSpec, fake_llm_client
from app.llm.types import ModelRole
from app.models.assessment import ItemType
from app.models.grading import GradingSnapshot
from app.models.learning import LearnerKCState, LearningEvent
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as svc
from app.services import regrade
from tests.test_grading_provenance import _setup

NOW = datetime.now(UTC).replace(tzinfo=None)
WINDOW = {"since": NOW - timedelta(days=1), "until": NOW + timedelta(days=1)}


def _grade(score: float) -> str:
    return json.dumps({"score": score, "rationale": "r"})


async def _graded(session: AsyncSession, score: float = 1.0, **setup):
    learner, kc, item = await _setup(session, **setup)
    await svc.answer_item(
        session, learner.id, item, AnswerSubmit(response={"text": "tip to tail"}),
        llm=fake_llm_client(_grade(score)),
    )
    return learner, kc, item


class _Recording(FakeProvider):
    def __init__(self, reply: str) -> None:
        super().__init__(reply=reply)
        self.systems: list[str | None] = []

    async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
        self.systems.append(system)
        return await super().complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def test_planning_finds_graded_attempts_and_calls_nothing(db_session) -> None:
    learner, _kc, _item = await _graded(db_session)
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    assert len(found.candidates) == 1 and found.by_grader == {"rubric": 1}
    assert found.not_regradable == 0


async def test_old_events_and_missing_snapshots_are_not_regradable(db_session) -> None:
    learner, kc, _item = await _graded(db_session)
    db_session.add(
        LearningEvent(
            learner_id=learner.id, kc_id=kc.id, event_type="observation",
            attempt_id=uuid.uuid4(), payload={"score": 1.0, "response": {"text": "x"}},
        )
    )
    await db_session.flush()
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    assert (len(found.candidates), found.not_regradable) == (1, 1)

    await db_session.execute(delete(GradingSnapshot).where(GradingSnapshot.learner_id == learner.id))
    await db_session.flush()
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    assert (len(found.candidates), found.not_regradable) == (0, 2)


async def test_a_disagreeing_grader_is_reported_and_nothing_changes(db_session) -> None:
    learner, kc, _item = await _graded(db_session, score=1.0)
    before = await db_session.scalar(
        select(LearnerKCState.ability).where(LearnerKCState.learner_id == learner.id)
    )
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)

    report = await regrade.run(fake_llm_client(_grade(0.0)), found)

    assert (report.compared, report.failed) == (1, 0)
    assert report.correct_agreement == 0.0
    assert report.mean_abs_score_diff == pytest.approx(1.0)
    assert [(c.recorded, c.regraded) for c in report.largest] == [(1.0, 0.0)]
    await db_session.refresh(
        await db_session.scalar(select(LearnerKCState).where(LearnerKCState.learner_id == learner.id))
    )
    after = await db_session.scalar(
        select(LearnerKCState.ability).where(LearnerKCState.learner_id == learner.id)
    )
    assert after == before


async def test_the_recorded_prompt_is_sent_when_asked(db_session, monkeypatch) -> None:
    learner, _kc, _item = await _graded(db_session)
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    monkeypatch.setattr(rubric_grading, "_SYSTEM_PROMPT", "TODAY'S PROMPT")

    recorded = _Recording(_grade(1.0))
    await regrade.run(_client(recorded), found, prompt="recorded")
    current = _Recording(_grade(1.0))
    await regrade.run(_client(current), found, prompt="current")

    assert recorded.systems[0] != "TODAY'S PROMPT" and "strict, fair grader" in recorded.systems[0]
    assert current.systems == ["TODAY'S PROMPT"]


async def test_the_rubric_reaches_only_its_own_component(db_session) -> None:
    """Review focus 2."""
    learner, _kc, _item = await _graded(db_session, rubric={"points": ["tip to tail"]})
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    [candidate] = found.candidates
    assert candidate.rubric is not None
    assert candidate.rubric["criteria"] == {"points": ["tip to tail"]}


async def test_an_empty_answer_regrades_to_zero_without_a_call(db_session) -> None:
    """Review focus 3."""
    learner, kc, item = await _setup(db_session)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"text": ""}),
        llm=fake_llm_client(_grade(1.0)),
    )
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    provider = _Recording(_grade(1.0))
    report = await regrade.run(_client(provider), found)
    assert provider.systems == [] and report.mean_abs_score_diff == 0.0


async def test_a_refused_regrade_is_counted_failed(db_session) -> None:
    """Review focus 5."""

    class Refusing(FakeProvider):
        async def complete(self, **kwargs):
            raise BudgetExceeded("deployment")

    learner, _kc, _item = await _graded(db_session)
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    report = await regrade.run(_client(Refusing()), found)
    assert (report.compared, report.failed) == (0, 1)


async def test_an_auto_grade_is_regraded_free(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice_index": 1}),
        llm=fake_llm_client(),
    )
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    provider = _Recording("unused")
    report = await regrade.run(_client(provider), found)
    assert provider.systems == [] and report.correct_agreement == 1.0


async def test_the_report_holds_no_learner_text(db_session) -> None:
    learner, _kc, _item = await _graded(db_session)
    found = await regrade.plan(db_session, learner_id=learner.id, **WINDOW)
    report = await regrade.run(fake_llm_client(_grade(0.0)), found)
    assert "tip to tail" not in regrade.render(found, report, None)
    assert "tip to tail" not in json.dumps(regrade.as_json(report), default=str)
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_regrade.py -q` → FAIL (`app.services.regrade` missing).

- [ ] **Step 3: Implement** `app/services/regrade.py`:

```python
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

from app.learning import rubric_grading
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
class Report:
    compared: int
    failed: int
    correct_agreement: float | None
    mean_abs_score_diff: float | None
    component_mean_abs_diff: float | None
    largest: list[Comparison]


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
                select(KC.id).join(Topic, KC.topic_id == Topic.id).where(
                    Topic.subject_id == subject_id
                )
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
        cost = price_usd(spec.provider, spec.model, Usage(input_tokens=chars // 4, output_tokens=512))
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
    by_kc = {
        entry["kc_id"]: entry["criteria"] for entry in (c.rubric or {}).get("components", [])
    }
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
        llm, stem=item["stem"], response=c.response, rubric=rubric, components=components,
        system=system,
    )
    return result


async def run(
    llm: LLMClient, found: Plan, *, prompt: Literal["current", "recorded"] = "current"
) -> Report:
    compared: list[tuple[Candidate, GradeResult]] = []
    failed = 0
    with attributed(feature="regrade", learner_id=None, conversation_id=None):
        for c in found.candidates:
            try:
                compared.append((c, await _regrade_one(llm, c, prompt)))
            except Exception:  # a refusal or provider failure is one failed comparison
                failed += 1
    if not compared:
        return Report(0, failed, None, None, None, [])
    diffs = [abs(c.score - r.score) for c, r in compared]
    component_diffs = [
        abs(score - r.component_scores[uuid.UUID(kc)])
        for c, r in compared
        for kc, score in c.component_scores.items()
        if uuid.UUID(kc) in r.component_scores
    ]
    largest = sorted(compared, key=lambda pair: abs(pair[0].score - pair[1].score), reverse=True)
    return Report(
        compared=len(compared),
        failed=failed,
        correct_agreement=sum(c.correct == r.correct for c, r in compared) / len(compared),
        mean_abs_score_diff=sum(diffs) / len(diffs),
        component_mean_abs_diff=(
            sum(component_diffs) / len(component_diffs) if component_diffs else None
        ),
        largest=[Comparison(c.event_id, c.score, r.score) for c, r in largest[:LARGEST]],
    )


def as_json(report: Report) -> dict:
    return {
        "compared": report.compared,
        "failed": report.failed,
        "correct_agreement": report.correct_agreement,
        "mean_abs_score_diff": report.mean_abs_score_diff,
        "component_mean_abs_diff": report.component_mean_abs_diff,
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
        f"  {c.event_id}: recorded {c.recorded:.2f} → {c.regraded:.2f}" for c in report.largest
    ]
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"
```

(`attributed(feature="regrade", learner_id=None, conversation_id=None)` clears any inherited learner, so the calls count only against the deployment ceiling. `rubric_grading` is imported so a monkeypatched `_SYSTEM_PROMPT` is read at call time — `grade_open` already reads the module global.)

`app/workers/regrade.py`:

```python
"""Put past answers through a grader again — ``uv run poe regrade [--run] [--since DAYS]
[--learner ID] [--subject ID] [--limit N] [--prompt current|recorded] [--model P:M]
[--out PATH]``. Dry run by default; never changes a grade. See docs/RUNBOOK.md §19."""

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.learning.rubric_grading import GRADING_ROLE
from app.llm.registry import ModelSpec, build_llm_client
from app.services import regrade
from app.services import spend_guard  # noqa: F401  installs the spend guard on the meter (S47)


async def run(args: argparse.Namespace) -> int:
    llm = build_llm_client(get_settings())
    if args.model:
        provider, _, model = args.model.partition(":")
        llm = llm.with_roles({GRADING_ROLE: ModelSpec(provider, model)})
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionFactory() as session:
        found = await regrade.plan(
            session,
            since=now - timedelta(days=args.since),
            until=now,
            learner_id=args.learner,
            subject_id=args.subject,
            limit=args.limit,
        )
    cost = regrade.estimate_cost(found, llm)
    report = await regrade.run(llm, found, prompt=args.prompt) if args.run else None
    print(regrade.render(found, report, cost))
    if report is not None:
        with open(args.out, "w") as handle:
            json.dump(regrade.as_json(report), handle, indent=2)
        print(f"report written to {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="make the grading calls (paid)")
    parser.add_argument("--since", type=int, default=7, help="days back (default 7)")
    parser.add_argument("--learner", type=uuid.UUID)
    parser.add_argument("--subject", type=uuid.UUID)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--prompt", choices=("current", "recorded"), default="current")
    parser.add_argument("--model", help="provider:model for the grader (default: current)")
    parser.add_argument("--out", default="regrade-report.json")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

`pyproject.toml`, beside `reindex`: `regrade = "python -m app.workers.regrade"`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_regrade.py tests/test_grading_provenance.py -q` → PASS; `uv run poe regrade --help` prints usage; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/regrade.py app/workers/regrade.py pyproject.toml tests/test_regrade.py
git status
git commit -m "feat(grading): poe regrade measures a grader against past answers [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Docs [S56]

- [ ] **Step 1: RUNBOOK** — `## 19. Grading provenance and re-grading (S56)`: what the `grading` block holds (grader, snapshot hashes, model, app version) and how to read a snapshot (`SELECT content FROM grading_snapshots WHERE learner_id = … AND sha256 = …`); bump `rubric_grading.TEMPLATE_VERSION` when `_build_prompt` changes; running `uv run poe regrade` (dry run, `--run`, `--prompt recorded`, `--model`, `--out`), what the numbers mean, that it is paid, counts against the deployment ceiling only, and never changes a grade; "not re-gradable" = written before event schema v5, self-rated, or a snapshot erased with the account.
- [ ] **Step 2: Tracker** — S56 next step: versioning part done (snapshots, `grading` block, `poe regrade`); remaining: explicit rubrics and difficulty targets for declared conversational checks (workstream 2 piece 2). Add the final review's deferred minors.
- [ ] **Step 3: CLAUDE.md** — a bullet after the S47/S48 one: "**A grade says what measured it** (S56) — every graded event carries a `grading` block (grader, model, and hashes of frozen item/rubric/prompt snapshots in `grading_snapshots`, per learner, surviving item deletion); `uv run poe regrade` re-grades past answers under a current or recorded grader and reports agreement, never changing a grade. See [docs/RUNBOOK.md](docs/RUNBOOK.md) §19."
- [ ] **Step 4: Commit**

```bash
git add docs/RUNBOOK.md docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record grading provenance and re-grading [S56]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
