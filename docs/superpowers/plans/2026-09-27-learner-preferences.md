# Learner Preferences Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Five explicit learner settings — guidance, explanation level, note format, hints, pace — with global defaults and subject overrides, that pin their parameter in every mode while inferred values stay visible.

**Architecture:** A code catalog (`app/learning/preferences.py`) plus one key/value table (`learner_preferences`, NULL subject = global) resolved by one function (`app/services/preferences.py` `effective`). Every consumer asks that function when it assembles instructions: the tutor context, lesson generation, the note-format cascade, and lesson-plan guidance. A small API and three UI surfaces (Account, lesson-plan panel, Dashboard) sit on top.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, pytest; React + TypeScript, TanStack Query, vitest.

**Spec:** `docs/superpowers/specs/2026-09-27-learner-preferences-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract` (after `uv run poe api-types`, stage `frontend/src/api/schema.d.ts`).
- Frontend changes also green on `cd frontend && npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npx prettier --check` and `npx eslint` on changed files (component files export only components).
- New migration → `uv run python -m tests.testdb`. Alembic revision ids must be ≤ 32 characters.
- Async tests: re-read with `populate_existing=True`; read ids into locals before code that may commit or roll back.
- Only catalog values ever reach a prompt; nothing free-text.
- Resolving preferences never makes a model call and never fails a turn (fall back to defaults, log).
- One tracker id per commit subject: `[S02]`.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls (tests use the fake LLM client).

## Review Focus

1. A learner changes a preference mid-conversation — the very next turn's system prompt reflects it; nothing cached (plan steps, lesson cache) serves the old setting (Task 4 test).
2. A subject override on a subject later deleted, or a stored value no longer in the catalog — resolution ignores it and falls back, never 500s (Task 1 test).
3. The old guidance endpoint and a plan regenerate — an existing learner's exploration choice survives the migration and a goal change (Tasks 1 and 3 tests).
4. Setting a subject override to the same value as the global default — it is kept as an override (so a later global change does not move it); only the key's catalog default deletes (Task 1 test).
5. A note with its own chosen format versus a `note_format` preference — the note's own choice wins; the preference beats the inferred format (Task 5 test).

---

### Task 1: Catalog, table, resolution, retention [S02]

**Files:**
- Create: `app/learning/preferences.py`, `app/models/preference.py`, `db/migrations/versions/0068_learner_preferences.py`, `app/services/preferences.py`, `tests/test_preferences.py`
- Modify: `app/models/__init__.py`, `app/services/retention.py` (`RETENTION` entry + export), `tests/test_migrations_with_data.py`

**Interfaces:**
- Produces:
  ```python
  # app/learning/preferences.py
  AUTO = "auto"
  CATALOG: dict[str, PreferenceSpec]   # keys: guidance, explanation_level, note_format, hints, pace
  class PreferenceSpec: key: str; options: tuple[str, ...]; default: str; label: str
  def is_valid(key: str, value: str) -> bool
  EXPLANATION_INSTRUCTIONS: dict[str, str]; PACE_INSTRUCTIONS: dict[str, str]
  HINT_INSTRUCTIONS: dict[str, str]; HINT_DENSITY: dict[str, str]  # fewer→low, some→medium, more→high
  # app/models/preference.py
  class LearnerPreference(learner_id, subject_id: uuid | None, key, value, created_at, updated_at)
  # app/services/preferences.py
  @dataclass(frozen=True) class Resolved: value: str; source: Literal["subject", "global", "default"]
  async def effective(session, learner_id, subject_id: uuid.UUID | None) -> dict[str, Resolved]
  async def values_for(session, learner_id, subject_id: uuid.UUID | None) -> dict[str, str]  # never raises
  async def guidance_for(session, learner_id, subject_id: uuid.UUID) -> Literal["guided", "exploration"]
  async def set_preference(session, learner_id, key, value, *, subject_id: uuid.UUID | None) -> None  # commits
  class InvalidPreference(ValueError)
  ```

- [ ] **Step 1: Failing tests** — `tests/test_preferences.py`:

```python
"""Explicit learner preferences: catalog, storage and resolution (S02, V09)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.preference import LearnerPreference
from app.services import preferences


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"p-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _subject(session: AsyncSession, learner: Learner) -> uuid.UUID:
    subject = Subject(name=f"S-{uuid.uuid4().hex[:6]}", owner_learner_id=learner.id)
    session.add(subject)
    await session.flush()
    return subject.id


async def test_nothing_set_resolves_to_the_defaults(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    got = await preferences.effective(db_session, learner.id, None)
    assert {k: (r.value, r.source) for k, r in got.items()} == {
        "guidance": ("guided", "default"),
        "explanation_level": ("auto", "default"),
        "note_format": ("auto", "default"),
        "hints": ("auto", "default"),
        "pace": ("auto", "default"),
    }


async def test_subject_beats_global_beats_default(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    await preferences.set_preference(
        db_session, learner.id, "pace", "unhurried", subject_id=subject_id
    )

    assert (await preferences.effective(db_session, learner.id, subject_id))["pace"] == (
        preferences.Resolved("unhurried", "subject")
    )
    assert (await preferences.effective(db_session, learner.id, None))["pace"] == (
        preferences.Resolved("brisk", "global")
    )
    other = await _subject(db_session, learner)
    assert (await preferences.effective(db_session, learner.id, other))["pace"] == (
        preferences.Resolved("brisk", "global")
    )


async def test_the_default_value_deletes_but_an_equal_override_stays(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(db_session, learner.id, "hints", "more", subject_id=None)
    await preferences.set_preference(db_session, learner.id, "hints", "more", subject_id=subject_id)
    await preferences.set_preference(db_session, learner.id, "hints", "fewer", subject_id=None)

    got = await preferences.effective(db_session, learner.id, subject_id)
    assert got["hints"] == preferences.Resolved("more", "subject"), "an override is pinned"

    await preferences.set_preference(db_session, learner.id, "hints", "auto", subject_id=subject_id)
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert [(r.subject_id, r.value) for r in rows] == [(None, "fewer")]


async def test_setting_twice_updates_the_one_row(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    await preferences.set_preference(db_session, learner.id, "pace", "unhurried", subject_id=None)
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert [r.value for r in rows] == ["unhurried"]


async def test_an_unknown_key_or_value_is_refused(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    with pytest.raises(preferences.InvalidPreference):
        await preferences.set_preference(db_session, learner.id, "font", "big", subject_id=None)
    with pytest.raises(preferences.InvalidPreference):
        await preferences.set_preference(db_session, learner.id, "pace", "warp", subject_id=None)


async def test_a_stored_value_that_left_the_catalog_is_ignored(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    db_session.add_all(
        [
            LearnerPreference(learner_id=learner.id, subject_id=None, key="pace", value="warp"),
            LearnerPreference(learner_id=learner.id, subject_id=None, key="font", value="big"),
        ]
    )
    await db_session.flush()
    got = await preferences.effective(db_session, learner.id, None)
    assert got["pace"] == preferences.Resolved("auto", "default")
    assert "font" not in got


async def test_deleting_a_subject_takes_its_overrides(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject_id = await _subject(db_session, learner)
    await preferences.set_preference(
        db_session, learner.id, "pace", "brisk", subject_id=subject_id
    )
    subject = await db_session.get(Subject, subject_id)
    await db_session.delete(subject)
    await db_session.commit()
    rows = (
        await db_session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner.id)
        )
    ).all()
    assert rows == []


async def test_values_for_never_raises(db_session: AsyncSession, monkeypatch) -> None:
    async def broken(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(preferences, "effective", broken)
    got = await preferences.values_for(db_session, uuid.uuid4(), None)
    assert got["guidance"] == "guided" and got["pace"] == "auto"


async def test_preferences_are_exported(db_session: AsyncSession) -> None:
    from app.services import retention

    learner = await _learner(db_session)
    await preferences.set_preference(db_session, learner.id, "pace", "brisk", subject_id=None)
    exported = await retention.export_learner(db_session, learner.id)
    assert [(p["key"], p["value"]) for p in exported["preferences"]] == [("pace", "brisk")]
```

(Check `Subject`'s required columns in `app/models/knowledge.py` and supply them if `name`/`owner_learner_id` are not enough.)

Append to `tests/test_migrations_with_data.py` (match the file's existing INSERT style for `subjects` and `lesson_plans`; read `app/models/lesson_plan.py` and `app/models/knowledge.py` for their NOT NULL columns):

```python
async def test_an_exploration_plan_becomes_a_subject_override() -> None:
    """0068 (S02): nobody's guidance changes because it moved to preferences."""
    async with database_at("0067_account_deletion_erasures") as connect:
        conn = await connect()
        try:
            learner_id, s1, s2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "prefs"
            )
            for sid in (s1, s2):
                await conn.execute(
                    "INSERT INTO subjects (id, name, owner_learner_id) VALUES ($1, $2, $3)",
                    sid,
                    f"s-{sid.hex[:6]}",
                    learner_id,
                )
            await conn.execute(
                "INSERT INTO lesson_plans (id, learner_id, subject_id, guidance) "
                "VALUES ($1, $2, $3, 'exploration'), ($4, $2, $5, 'guided')",
                uuid.uuid4(),
                learner_id,
                s1,
                uuid.uuid4(),
                s2,
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0068_learner_preferences")

        conn = await connect()
        try:
            rows = await conn.fetch(
                "SELECT subject_id, key, value FROM learner_preferences WHERE learner_id = $1",
                learner_id,
            )
            assert [(r["subject_id"], r["key"], r["value"]) for r in rows] == [
                (s1, "guidance", "exploration")
            ]
        finally:
            await conn.close()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_preferences.py -q` → FAIL (`ModuleNotFoundError: app.models.preference`).

- [ ] **Step 3: Implement.**

`app/learning/preferences.py`:

```python
"""What a learner can set explicitly, and what each setting tells the model (S02, V09).

A catalog in code, like the profile's dimension catalog: adding a setting needs no migration.
An explicit setting *pins* its parameter — inference keeps computing and showing its own value,
but stops steering. ``auto`` means "adapt to me", which is also what an unset key resolves to.

Only these fixed strings ever reach a prompt. A setting is chosen from a list, so nothing a
learner types can be quoted back to a model as an instruction.
"""

from dataclasses import dataclass

AUTO = "auto"


@dataclass(frozen=True)
class PreferenceSpec:
    key: str
    options: tuple[str, ...]
    default: str
    label: str


CATALOG: dict[str, PreferenceSpec] = {
    spec.key: spec
    for spec in (
        # Nothing infers guidance (V07), so it has no "adapt to me": guided is the default.
        PreferenceSpec("guidance", ("guided", "exploration"), "guided", "Guidance"),
        PreferenceSpec(
            "explanation_level",
            (AUTO, "introductory", "standard", "advanced"),
            AUTO,
            "Explanation level",
        ),
        PreferenceSpec(
            "note_format",
            (AUTO, "outline", "narrative", "mnemonic", "worked_examples"),
            AUTO,
            "Note format",
        ),
        PreferenceSpec("hints", (AUTO, "fewer", "some", "more"), AUTO, "Hints"),
        PreferenceSpec("pace", (AUTO, "brisk", "standard", "unhurried"), AUTO, "Pace"),
    )
}


def is_valid(key: str, value: str) -> bool:
    spec = CATALOG.get(key)
    return spec is not None and value in spec.options


EXPLANATION_INSTRUCTIONS = {
    "introductory": "Pitch explanations at an introductory level: define terms as they come up "
    "and assume no background in the subject.",
    "standard": "Pitch explanations at a standard level: assume the usual background for this "
    "subject, and define anything specialised.",
    "advanced": "Pitch explanations at an advanced level: be concise, skip the basics, and go "
    "into depth.",
}

PACE_INSTRUCTIONS = {
    "brisk": "Keep a brisk pace: short steps, and move on as soon as they have it.",
    "standard": "Keep a steady pace.",
    "unhurried": "Take an unhurried pace: smaller steps, and check understanding before moving "
    "on.",
}

HINT_INSTRUCTIONS = {
    "fewer": "Give fewer hints: let them try first, and hint only when asked or clearly stuck.",
    "some": "Give hints at a moderate rate.",
    "more": "Give hints readily: offer a nudge as soon as they hesitate.",
}

# The plan's inferred hint density uses low/medium/high; the learner-facing words differ.
HINT_DENSITY = {"fewer": "low", "some": "medium", "more": "high"}
```

`app/models/preference.py`:

```python
"""A learner's explicit settings (S02, V09): one row per choice, NULL subject = global.

A row exists only for an explicit choice. Setting a key back to its catalog default deletes the
row at that level, so "adapt to me" is the absence of a row rather than a stored value.
"""

import uuid

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LearnerPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "learner_preferences"
    __table_args__ = (
        Index(
            "uq_learner_preferences_scope_key",
            "learner_id",
            "subject_id",
            "key",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), default=None
    )
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
```

Register in `app/models/__init__.py` (import + `__all__`, sorted).

Migration `0068_learner_preferences.py` (`down_revision = "0067_account_deletion_erasures"`):

```python
"""Explicit learner preferences, global and per subject (S02, V09).

Backfills each lesson plan's non-default guidance as a subject override, so nobody's choice
changes. A plan on the default needs no row: it resolves to guided either way. The old column
cannot tell a plan explicitly switched back to guided from one never touched, so neither gets a
row (see the spec). ``lesson_plans.guidance`` stays, unread, until a later cleanup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_learner_preferences"
down_revision: str | Sequence[str] | None = "0067_account_deletion_erasures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learner_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learner_preferences_learner_id", "learner_preferences", ["learner_id"])
    op.create_index(
        "uq_learner_preferences_scope_key",
        "learner_preferences",
        ["learner_id", "subject_id", "key"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.execute(
        "INSERT INTO learner_preferences (id, learner_id, subject_id, key, value) "
        "SELECT gen_random_uuid(), learner_id, subject_id, 'guidance', guidance "
        "FROM lesson_plans WHERE guidance <> 'guided'"
    )


def downgrade() -> None:
    op.drop_index("uq_learner_preferences_scope_key", table_name="learner_preferences")
    op.drop_index("ix_learner_preferences_learner_id", table_name="learner_preferences")
    op.drop_table("learner_preferences")
```

(Match `TimestampMixin`'s column types — it maps `created_at`/`updated_at` as plain `DateTime`; if other migrations create them differently, copy theirs.)

`app/services/preferences.py`:

```python
"""Resolve a learner's explicit settings (S02, V09): subject override → global → default.

The one place any consumer asks — the tutor context, lesson generation, the note-format cascade
and lesson-plan guidance — so they cannot disagree about what the learner chose.
"""

import uuid
from dataclasses import dataclass
from typing import Literal, cast

import structlog
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.preferences import CATALOG, is_valid
from app.models.preference import LearnerPreference

log = structlog.get_logger(__name__)

Source = Literal["subject", "global", "default"]


class InvalidPreference(ValueError):
    """A key not in the catalog, or a value not among that key's options."""


@dataclass(frozen=True)
class Resolved:
    value: str
    source: Source


def _defaults() -> dict[str, Resolved]:
    return {key: Resolved(spec.default, "default") for key, spec in CATALOG.items()}


async def effective(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID | None
) -> dict[str, Resolved]:
    """Every catalog key, resolved. One query, no model call.

    A stored row whose key or value is no longer in the catalog is skipped, never an error:
    the catalog is expected to change, and a stale row must not break a turn.
    """
    scope = LearnerPreference.subject_id.is_(None)
    if subject_id is not None:
        scope = or_(scope, LearnerPreference.subject_id == subject_id)
    rows = (
        await session.scalars(
            select(LearnerPreference).where(LearnerPreference.learner_id == learner_id, scope)
        )
    ).all()
    resolved = _defaults()
    # Global first, then subject, so the subject row overwrites.
    for row in sorted(rows, key=lambda r: r.subject_id is not None):
        if is_valid(row.key, row.value):
            resolved[row.key] = Resolved(row.value, "subject" if row.subject_id else "global")
    return resolved


async def values_for(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID | None
) -> dict[str, str]:
    """Just the values, for prompt assembly. Never raises: a failed read gives the defaults."""
    try:
        resolved = await effective(session, learner_id, subject_id)
    except Exception:
        log.exception("preferences.read_failed", learner_id=str(learner_id))
        resolved = _defaults()
    return {key: r.value for key, r in resolved.items()}


async def guidance_for(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> Literal["guided", "exploration"]:
    value = (await effective(session, learner_id, subject_id))["guidance"].value
    return cast("Literal['guided', 'exploration']", value)


async def set_preference(
    session: AsyncSession,
    learner_id: uuid.UUID,
    key: str,
    value: str,
    *,
    subject_id: uuid.UUID | None,
) -> None:
    """Record an explicit choice at one level, or clear it with the key's default. Commits.

    Only the catalog default deletes: an override equal to the global value is still an
    override, so a later change of the global default does not move this subject.
    """
    if not is_valid(key, value):
        raise InvalidPreference(f"{key}={value}")
    at_level = (
        LearnerPreference.subject_id.is_(None)
        if subject_id is None
        else LearnerPreference.subject_id == subject_id
    )
    if value == CATALOG[key].default:
        await session.execute(
            delete(LearnerPreference).where(
                LearnerPreference.learner_id == learner_id, LearnerPreference.key == key, at_level
            )
        )
    else:
        await session.execute(
            pg_insert(LearnerPreference)
            .values(id=uuid.uuid4(), learner_id=learner_id, subject_id=subject_id, key=key, value=value)
            .on_conflict_do_update(
                index_elements=["learner_id", "subject_id", "key"],
                set_={"value": value, "updated_at": func.now()},
            )
        )
    await session.commit()
```

(Import `func` from sqlalchemy. `on_conflict_do_update(index_elements=...)` infers the unique index; because it is `NULLS NOT DISTINCT`, a second global row conflicts too — `test_setting_twice_updates_the_one_row` proves it. If Postgres will not infer the index for the NULL-subject case, replace the upsert with a delete of the row at that level followed by an insert, in the same transaction, and ledger the ruling.)

`app/services/retention.py`: add after the `memories` entry

```python
    StoreRetention(
        "learner_preferences",
        "deleted",
        "Cascades from the learner; a subject override also goes with its subject (S02).",
    ),
```

and in `export_learner`'s dict: `"preferences": await rows(LearnerPreference, LearnerPreference.learner_id == learner_id),` (import the model).

- [ ] **Step 4: Run** — `uv run python -m tests.testdb`; `uv run pytest tests/test_preferences.py tests/test_migrations_with_data.py tests/test_retention.py -q` → PASS. `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/learning/preferences.py app/models/preference.py app/models/__init__.py db/migrations/versions/0068_learner_preferences.py app/services/preferences.py app/services/retention.py tests/test_preferences.py tests/test_migrations_with_data.py
git status
git commit -m "feat(preferences): explicit settings, global and per subject [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The preferences API [S02]

**Files:**
- Create: `app/api/v1/preferences.py`, `app/schemas/preference.py`
- Modify: `app/main.py` (or wherever v1 routers are included — follow `retention`'s registration), `tests/test_visibility_sweep.py` (two `Case`s), `frontend/src/api/schema.d.ts`
- Test: `tests/test_preferences_api.py` (create)

**Interfaces:**
- Consumes: `preferences.effective`, `set_preference`, `InvalidPreference`, `CATALOG`; `knowledge_svc.require_visible_subject` (raises `NotVisible` → 404 via `app/main.py`'s handler); `profile_svc.get_snapshot`; `app.learning.lesson_plan.scaffolding_from_profile`.
- Produces: `GET /api/v1/preferences?subject_id=` → `list[PreferenceRead]`; `PUT /api/v1/preferences/{key}` body `PreferenceSubmit` → `PreferenceRead`.
  ```python
  class PreferenceRead(BaseModel):
      key: str; label: str; value: str; source: Literal["subject", "global", "default"]
      options: list[str]; global_value: str | None; inferred: str | None
  class PreferenceSubmit(BaseModel):
      value: str; subject_id: uuid.UUID | None = None
  ```

- [ ] **Step 1: Failing tests** — `tests/test_preferences_api.py`:

```python
"""The preferences API (S02, V09)."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.profile import LearnerProfile, ProfileDimension

API = "/api/v1"


async def _subject(session: AsyncSession, owner: Learner | None) -> uuid.UUID:
    subject = Subject(
        name=f"S-{uuid.uuid4().hex[:6]}", owner_learner_id=owner.id if owner else None
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def test_every_setting_is_listed_with_where_it_came_from(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject_id = await _subject(db_session, api_learner)
    await api_client.put(f"{API}/preferences/pace", json={"value": "brisk"})

    listed = (await api_client.get(f"{API}/preferences", params={"subject_id": str(subject_id)})).json()
    by_key = {p["key"]: p for p in listed}
    assert set(by_key) == {"guidance", "explanation_level", "note_format", "hints", "pace"}
    assert by_key["pace"]["value"] == "brisk" and by_key["pace"]["source"] == "global"
    assert by_key["pace"]["global_value"] == "brisk"
    assert by_key["pace"]["options"] == ["auto", "brisk", "standard", "unhurried"]

    r = await api_client.put(
        f"{API}/preferences/pace", json={"value": "unhurried", "subject_id": str(subject_id)}
    )
    assert r.status_code == 200
    assert r.json()["value"] == "unhurried" and r.json()["source"] == "subject"
    assert r.json()["global_value"] == "brisk"


async def test_an_inferred_value_is_shown_beside_the_setting(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    profile = LearnerProfile(learner_id=api_learner.id)
    db_session.add(profile)
    await db_session.flush()
    db_session.add(
        ProfileDimension(
            learner_id=api_learner.id,
            profile_id=profile.id,
            key="note_format",
            value="mnemonic",
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()

    listed = (await api_client.get(f"{API}/preferences")).json()
    by_key = {p["key"]: p for p in listed}
    assert by_key["note_format"]["inferred"] == "mnemonic"
    assert by_key["explanation_level"]["inferred"] is None


async def test_bad_keys_values_and_subjects_are_refused(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    stranger = Learner(handle=f"x-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    theirs = await _subject(db_session, stranger)
    curated = await _subject(db_session, None)

    assert (await api_client.put(f"{API}/preferences/font", json={"value": "x"})).status_code == 422
    assert (await api_client.put(f"{API}/preferences/pace", json={"value": "warp"})).status_code == 422
    assert (
        await api_client.put(
            f"{API}/preferences/pace", json={"value": "brisk", "subject_id": str(theirs)}
        )
    ).status_code == 404
    assert (
        await api_client.get(f"{API}/preferences", params={"subject_id": str(theirs)})
    ).status_code == 404
    assert (
        await api_client.put(
            f"{API}/preferences/pace", json={"value": "brisk", "subject_id": str(curated)}
        )
    ).status_code == 200
```

(Read `ProfileDimension`/`LearnerProfile` for their required columns and adjust the constructor; the point is one stored `note_format` dimension.)

Add a sudo-audit test to `tests/test_admin_sudo.py`, mirroring `test_sudo_can_write_and_records_operator_target_reason_and_result`:

```python
async def test_a_visit_may_set_a_preference_and_it_is_audited(
    admin_client: AsyncClient, anon_client: AsyncClient, api_learner: Learner
) -> None:
    _, visit = await _visit(admin_client, api_learner)
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    r = await anon_client.put("/api/v1/preferences/pace", json={"value": "brisk"})
    assert r.status_code == 200, r.text
    log = await admin_client.get(
        f"/api/v1/admin/impersonations/{visit['impersonation']['id']}/actions"
    )
    assert any(a["route"] == "/api/v1/preferences/{key}" or "preferences" in a["route"] for a in log.json())
```

Add to `CASES` in `tests/test_visibility_sweep.py`, near the other `subject_id` cases:

```python
    Case(
        "GET",
        "/api/v1/preferences",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/preferences", params={"subject_id": str(t.subject)}),
    ),
    Case(
        "PUT",
        "/api/v1/preferences/{key}",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.put(
            f"{API}/preferences/pace", json={"value": "brisk", "subject_id": str(t.subject)}
        ),
    ),
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_preferences_api.py -q` → FAIL (404 on `/preferences`).

- [ ] **Step 3: Implement.**

`app/schemas/preference.py` — the two models above, with a docstring line each.

`app/api/v1/preferences.py`:

```python
"""A learner's explicit settings (S02, V09): read with provenance, set per level."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, SessionDep
from app.learning import lesson_plan as plan_engine
from app.learning.preferences import CATALOG, HINT_DENSITY
from app.schemas.preference import PreferenceRead, PreferenceSubmit
from app.services import knowledge as knowledge_svc
from app.services import preferences as svc
from app.services import profile as profile_svc

router = APIRouter(tags=["preferences"])

_HINT_WORD = {density: word for word, density in HINT_DENSITY.items()}


async def _inferred(session, learner_id: uuid.UUID) -> dict[str, str | None]:
    """What inference would choose for the keys it covers — shown, never used while pinned."""
    snapshot = await profile_svc.get_snapshot(session, learner_id)
    values = {d.key: d.value for d in snapshot}
    hints = plan_engine.scaffolding_from_profile(values)
    note_format = values.get("note_format")
    return {
        "hints": _HINT_WORD.get(hints.hint_density or ""),
        # ScaffoldingHints.pacing defaults to "standard" even with no evidence, so only report
        # a pace when the dimension exists.
        "pace": hints.pacing if isinstance(values.get("pace"), dict) else None,
        "note_format": note_format
        if isinstance(note_format, str) and note_format in CATALOG["note_format"].options
        else None,
    }


async def _read(session, learner_id: uuid.UUID, subject_id: uuid.UUID | None) -> list[PreferenceRead]:
    resolved = await svc.effective(session, learner_id, subject_id)
    global_level = (
        await svc.effective(session, learner_id, None) if subject_id is not None else None
    )
    inferred = await _inferred(session, learner_id)
    return [
        PreferenceRead(
            key=key,
            label=spec.label,
            value=resolved[key].value,
            source=resolved[key].source,
            options=list(spec.options),
            global_value=global_level[key].value if global_level is not None else None,
            inferred=inferred.get(key),
        )
        for key, spec in CATALOG.items()
    ]


@router.get("/preferences", response_model=list[PreferenceRead])
async def list_preferences(
    session: SessionDep, learner: CurrentLearner, subject_id: uuid.UUID | None = None
):
    """Every setting, what it resolves to here, and where that came from."""
    if subject_id is not None:
        await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    return await _read(session, learner.id, subject_id)


@router.put("/preferences/{key}", response_model=PreferenceRead)
async def set_preference(
    key: str, data: PreferenceSubmit, session: SessionDep, learner: CurrentLearner
):
    """Pin a setting globally or for one subject; the key's default clears that level."""
    if data.subject_id is not None:
        await knowledge_svc.require_visible_subject(session, data.subject_id, learner.id)
    learner_id = learner.id
    try:
        await svc.set_preference(session, learner_id, key, data.value, subject_id=data.subject_id)
    except svc.InvalidPreference as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "invalid_preference", "message": f"Not a setting: {exc}"},
        ) from exc
    [entry] = [p for p in await _read(session, learner_id, data.subject_id) if p.key == key]
    return entry
```

(Type the `session` parameters as `AsyncSession`. Register the router exactly as `app/api/v1/retention.py`'s router is registered.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_preferences_api.py tests/test_visibility_sweep.py tests/test_admin_sudo.py -q` → PASS. `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/preferences.py app/schemas/preference.py app/main.py tests/test_preferences_api.py tests/test_visibility_sweep.py tests/test_admin_sudo.py frontend/src/api/schema.d.ts
git status
git commit -m "feat(preferences): read and set explicit settings, with what inference would choose [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Stage whichever file registers routers, if not `app/main.py`.)

---

### Task 3: Guidance reads the preference [S02]

**Files:**
- Modify: `app/services/lesson_plan.py` (`generate_lesson_plan`, `_apply_revision`, `set_guidance`, `plan_read`)
- Test: `tests/test_preferences.py` (append) or the existing guidance tests — find them with `grep -rln "set_guidance\|lesson-plan/guidance" tests`

**Interfaces:**
- Consumes: `preferences.guidance_for(session, learner_id, subject_id)`, `preferences.set_preference`.
- Produces: `LessonPlanRead.guidance` = the effective value; `set_guidance` writes the subject override.

- [ ] **Step 1: Failing tests** — append to `tests/test_preferences.py` (build a plan the way the existing guidance tests do; reuse their helper by importing it if it is a module-level function, otherwise copy its setup):

```python
async def test_the_guidance_endpoint_writes_the_subject_override(
    api_client, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject_id = await _plan_for(db_session, api_learner)  # a subject with a lesson plan

    r = await api_client.patch(
        f"/api/v1/subjects/{subject_id}/lesson-plan/guidance", json={"guidance": "exploration"}
    )

    assert r.status_code == 200 and r.json()["guidance"] == "exploration"
    got = await preferences.effective(db_session, api_learner.id, subject_id)
    assert got["guidance"] == preferences.Resolved("exploration", "subject")


async def test_a_global_default_reaches_a_plan_with_no_override(
    api_client, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject_id = await _plan_for(db_session, api_learner)
    await preferences.set_preference(
        db_session, api_learner.id, "guidance", "exploration", subject_id=None
    )

    r = await api_client.get(f"/api/v1/subjects/{subject_id}/lesson-plan")

    assert r.json()["guidance"] == "exploration"
```

Also add a test that a detour under a subject override of `exploration` is *proposed* (status `proposed`) while the plan row's own `guidance` column still says `guided` — copy the setup of the existing exploration-detour test (grep `"proposed"` in `tests/test_lesson_plan*.py`) and set the preference instead of the column.

- [ ] **Step 2: Run to verify failure** — the new tests → FAIL (preference not written / column read).

- [ ] **Step 3: Implement** in `app/services/lesson_plan.py` (import `from app.services import preferences as preferences_svc`):

- `generate_lesson_plan`: replace `guidance = cast("engine.Guidance", plan.guidance) if plan is not None else "guided"` and its comment with
  ```python
      # The learner's setting for this subject, else their default (S02). Read from
      # preferences, not the plan row, so a regenerate keeps it and a global change reaches it.
      guidance = await preferences_svc.guidance_for(session, learner_id, subject_id)
  ```
  (keep the `plan = await _get_plan(...)` line if later code uses `plan`).
- `_apply_revision`: at its top, `guidance = await preferences_svc.guidance_for(session, learner_id, plan.subject_id)`; replace the three `cast("engine.Guidance", plan.guidance)` with `guidance` and `proposed=plan.guidance == "exploration"` with `proposed=guidance == "exploration"`.
- `set_guidance`: after the `plan is None` check, `await preferences_svc.set_preference(session, learner_id, "guidance", guidance, subject_id=subject_id)` instead of assigning the column; then `await session.refresh(plan)` and return. Update its docstring: it writes the subject's guidance preference (S02); no step is rewritten.
- `plan_read`: add `"guidance": await preferences_svc.guidance_for(session, plan.learner_id, plan.subject_id)` to the `model_copy(update=...)`.
- `app/models/lesson_plan.py`: rewrite the `guidance` column comment to say it is no longer read since S02 (preferences hold it) and stays until a cleanup migration.

- [ ] **Step 4: Run** — `uv run pytest tests/test_preferences.py tests/test_lesson_plan*.py tests/test_detour*.py -q` (whatever exists) → PASS; `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/lesson_plan.py app/models/lesson_plan.py tests/test_preferences.py
git status
git commit -m "feat(preferences): lesson-plan guidance follows the learner's setting [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The tutor and lessons obey explanation level, pace and hints [S02]

**Files:**
- Modify: `app/services/learner_context.py` (`LearnerContext.preferences`, `gather`, `compose`, `plan_note`, new `settings_note`), `app/services/content.py` (`generate_block`)
- Test: `tests/test_learner_context.py`, `tests/test_content.py` (or wherever `generate_block` is tested — `grep -rln "generate_block" tests`)

**Interfaces:**
- Consumes: `preferences.values_for`; `EXPLANATION_INSTRUCTIONS`, `PACE_INSTRUCTIONS`, `HINT_INSTRUCTIONS`, `AUTO`.
- Produces: `LearnerContext(goal, plan, memories, preferences: Mapping[str, str] = {})`; `settings_note(preferences) -> str | None`; `plan_note(plan, *, task_fixed=False, hints_pinned=False)`.

- [ ] **Step 1: Failing tests** — append to `tests/test_learner_context.py`:

```python
def _plan() -> PlanGroundingContext:
    return PlanGroundingContext(
        subject_name="Subj",
        kc_id=uuid.uuid4(),
        kc_name="KCNAME",
        step_type="new",
        target_difficulty=None,
        hint_density="high",
        preferred_item_type=None,
    )


def test_pinned_settings_reach_the_prompt_and_pinned_hints_replace_the_inferred() -> None:
    context = LearnerContext(
        goal=None,
        plan=_plan(),
        memories=[],
        preferences={"explanation_level": "introductory", "pace": "brisk", "hints": "fewer"},
    )
    system = learner_context.compose("BASE", context)
    assert "introductory level" in system
    assert "brisk pace" in system
    assert "fewer hints" in system.lower()
    assert "Hint density: high" not in system, "the learner's setting pins hints"


def test_adapt_to_me_leaves_the_prompt_as_it_was() -> None:
    auto = {"explanation_level": "auto", "pace": "auto", "hints": "auto"}
    with_auto = LearnerContext(goal=None, plan=_plan(), memories=[], preferences=auto)
    without = LearnerContext(goal=None, plan=_plan(), memories=[])
    assert learner_context.compose("BASE", with_auto) == learner_context.compose("BASE", without)
    assert "Hint density: high" in learner_context.compose("BASE", with_auto)


async def test_a_changed_setting_reaches_the_next_turn(db_session: AsyncSession) -> None:
    """Read at assembly time: nothing cached between turns keeps the old setting."""
    from app.services import preferences

    learner, conversation, _kc = await _learner_with_everything(db_session)
    llm = fake_llm_client()
    before = await learner_context.gather(
        db_session, llm, learner_id=learner.id, conversation=conversation, query="q"
    )
    await preferences.set_preference(
        db_session, learner.id, "explanation_level", "advanced", subject_id=conversation.subject_id
    )
    after = await learner_context.gather(
        db_session, llm, learner_id=learner.id, conversation=conversation, query="q"
    )
    assert "advanced level" not in learner_context.compose("B", before)
    assert "advanced level" in learner_context.compose("B", after)
```

(`_learner_with_everything` is this file's existing helper; if it commits nothing, `set_preference`'s commit is fine inside the test transaction as elsewhere in the suite.)

In the `generate_block` test file, add:

```python
async def test_a_pinned_explanation_level_shapes_the_lesson_and_its_cache_key(
    db_session: AsyncSession,
) -> None:
    """A block cached at another level is never served: the level is in the system prompt,
    and the cache key covers the rendered system prompt."""
    from app.services import preferences

    # Build a learner + KC exactly as this file's existing generate_block tests do.
    learner_id, kc_id, subject_id = await _learner_and_kc(db_session)
    first = await content.generate_block(db_session, fake_llm_client(), learner_id=learner_id, kc_id=kc_id, block_type=ContentType.LESSON)
    await preferences.set_preference(
        db_session, learner_id, "explanation_level", "introductory", subject_id=subject_id
    )
    second = await content.generate_block(db_session, fake_llm_client(), learner_id=learner_id, kc_id=kc_id, block_type=ContentType.LESSON)
    assert first.cache_key != second.cache_key
```

(Adapt names — `_learner_and_kc`, `ContentType.LESSON`, the `generate_block` keyword names — to that file's existing setup; read its first `generate_block` test and copy its arrangement.)

- [ ] **Step 2: Run to verify failure** — the new tests → FAIL (`LearnerContext` has no `preferences`; same cache key).

- [ ] **Step 3: Implement.**

`app/services/learner_context.py`:

```python
@dataclass(frozen=True)
class LearnerContext:
    """The learner-facing state a turn should carry, whichever graph is running."""

    goal: str | None
    plan: PlanGroundingContext | None
    memories: Sequence[MemoryHit]
    # The learner's explicit settings for this conversation's subject (S02): resolved values,
    # "auto" where they left it to adaptation.
    preferences: Mapping[str, str] = field(default_factory=dict)
```

In `gather`, before the return: `settings = await preferences_svc.values_for(session, learner_id, conversation.subject_id)` and pass `preferences=settings`.

`compose`: after the plan focus,

```python
    focus = plan_note(context.plan, task_fixed=task_fixed, hints_pinned=_pinned(context, "hints"))
    if focus is not None:
        parts.append(focus)
    settings = settings_note(context.preferences)
    if settings is not None:
        parts.append(settings)
```

`plan_note(context, *, task_fixed=False, hints_pinned=False)`: wrap the hint-density line in `if context.hint_density is not None and not hints_pinned:`.

New:

```python
def _pinned(context: LearnerContext, key: str) -> bool:
    return context.preferences.get(key, AUTO) != AUTO


def settings_note(preferences: Mapping[str, str]) -> str | None:
    """The learner's own settings, as instructions, or ``None`` when they left all to adaptation.

    Catalog strings only (``app.learning.preferences``) — nothing the learner typed — so this
    is not fenced as untrusted the way memory is.
    """
    lines = [
        table[value]
        for key, table in (
            ("explanation_level", EXPLANATION_INSTRUCTIONS),
            ("pace", PACE_INSTRUCTIONS),
            ("hints", HINT_INSTRUCTIONS),
        )
        if (value := preferences.get(key, AUTO)) in table
    ]
    if not lines:
        return None
    return "The learner's own settings (these take precedence): " + " ".join(lines)
```

Update the module docstring's order line and `compose`'s docstring: `base -> goal -> plan focus -> learner settings -> extra -> grounding -> memory`.

`app/services/content.py` `generate_block`: after `system` is final and before `_cache_key`,

```python
    # The learner's explanation level for this subject (S02). Part of the system prompt, so the
    # cache key — which covers the rendered prompt — separates blocks written at other levels.
    level = (await preferences_svc.values_for(session, learner_id, subject_id))["explanation_level"]
    if level in EXPLANATION_INSTRUCTIONS:
        system = f"{system} {EXPLANATION_INSTRUCTIONS[level]}"
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_learner_context.py <the content test file> -q` → PASS; `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/learner_context.py app/services/content.py tests/test_learner_context.py <the content test file>
git status
git commit -m "feat(preferences): the tutor and lessons follow explanation level, pace and hints [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Note format follows the preference [S02]

**Files:**
- Modify: `app/services/notes.py` (`_format_inputs`, `_format_from`, `effective_format`, their callers, `notes_index`)
- Test: the notes format tests — `grep -rln "effective_format\|_format_from" tests`

**Interfaces:**
- Consumes: `preferences.values_for`.
- Produces: `effective_format(session, learner_id, topic: Topic, note: Note | None) -> str`; `_format_inputs(session, learner_id, subject_id) -> tuple[object, object, object]` (preferred, learned, conceptual); `_format_from(note, preferred, learned, conceptual) -> str`.

- [ ] **Step 1: Failing tests** — in the notes test file (copy its arrangement for a learner, topic and note):

```python
async def test_the_note_format_order(db_session: AsyncSession) -> None:
    """The note's own format > the learner's setting > the inferred format > heuristic."""
    from app.services import notes, preferences

    learner, topic = await _learner_and_topic(db_session)  # this file's setup
    await _set_dimension(db_session, learner.id, "note_format", "mnemonic")  # this file's helper, or insert a ProfileDimension
    assert await notes.effective_format(db_session, learner.id, topic, None) == "mnemonic"

    await preferences.set_preference(
        db_session, learner.id, "note_format", "narrative", subject_id=topic.subject_id
    )
    assert await notes.effective_format(db_session, learner.id, topic, None) == "narrative"

    note = await _note(db_session, learner, topic, format="worked_examples")  # this file's setup
    assert await notes.effective_format(db_session, learner.id, topic, note) == "worked_examples"
```

Also assert `notes_index` reports `narrative` for that topic when no note exists (add it to the same test, reading the index the way the existing index test does).

- [ ] **Step 2: Run to verify failure** → FAIL (`effective_format` takes 3 arguments / returns `mnemonic`).

- [ ] **Step 3: Implement** in `app/services/notes.py`:

```python
async def _format_inputs(
    session: AsyncSession, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> tuple[object, object, object]:
    """The subject-wide values the format cascade reads: the learner's setting (S02), the
    learned dimension, and the conceptual-error share. Fetched once per subject (S62)."""
    preferred = (await preferences_svc.values_for(session, learner_id, subject_id))["note_format"]
    learned = await _dimension_value(session, learner_id, "note_format")
    errors = await _dimension_value(session, learner_id, "error_type")
    return preferred, learned, errors.get("conceptual", 0) if isinstance(errors, dict) else None


def _format_from(note: Note | None, preferred: object, learned: object, conceptual: object) -> str:
    """The cascade over values in hand: the note's own choice > the learner's setting > the
    learned dimension > heuristic > outline."""
    if note is not None and note.format:
        return note.format
    if isinstance(preferred, str) and preferred in FORMATS:
        return preferred
    if isinstance(learned, str) and learned in FORMATS:
        return learned
    if isinstance(conceptual, int | float) and conceptual >= 0.5:
        return "worked_examples"
    return FALLBACK_FORMAT


async def effective_format(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic, note: Note | None
) -> str:
    """The cascade: the note's own choice > setting > learned dimension > heuristic > outline."""
    if note is not None and note.format:
        return note.format
    return _format_from(note, *await _format_inputs(session, learner_id, topic.subject_id))
```

Update every `effective_format(session, learner_id, note)` call to pass `topic` (each caller has `topic` in scope — `_view`, `refresh_note`, `absorb_edit`, `set_format`, `restore_revision`); in `notes_index` pass `subject_id` to `_format_inputs` and unpack three values into `_format_from`. ("auto" is not in `FORMATS`, so an unset preference falls through.)

- [ ] **Step 4: Run** — the notes tests → PASS; `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/notes.py <the notes test file>
git status
git commit -m "feat(preferences): note format follows the learner's setting [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Frontend — preferences on Account, per subject, and on the Dashboard [S02]

**Files:**
- Create: `frontend/src/components/preferences/PreferenceControls.tsx`, `frontend/src/components/preferences/PreferenceControls.test.tsx`, `frontend/src/components/preferences/preferenceCopy.ts`
- Modify: `frontend/src/api/hooks.ts` (`usePreferences`, `useSetPreference`), `frontend/src/pages/Account.tsx` (Preferences section), `frontend/src/components/lessons/LessonPlanPanel.tsx` (replace the `GuidanceToggle` block with `<PreferenceControls subjectId={subjectId} />`), `frontend/src/components/dashboard/ProfileSection.tsx` (override label)
- Test: `PreferenceControls.test.tsx`, `ProfileSection` label test in a new `frontend/src/components/dashboard/ProfileSection.test.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/preferences`, `PUT /api/v1/preferences/{key}` (Task 2 schema types `components["schemas"]["PreferenceRead"]`).
- Produces: `PreferenceControls({ subjectId?: string })`; hooks `usePreferences(subjectId?)`, `useSetPreference(subjectId?)`; `preferenceCopy.ts` exports `OPTION_LABELS: Record<string, string>` and `OVERRIDDEN_DIMENSIONS: Record<string, string>` (profile dimension key → preference key: `help_seeking`/`persistence` → `hints`, `pace` → `pace`, `note_format` → `note_format`).

- [ ] **Step 1: Failing tests** — `PreferenceControls.test.tsx`:

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const setPref = vi.fn();
let rows: unknown[] = [];
vi.mock("../../api/hooks", () => ({
  usePreferences: () => ({ data: rows }),
  useSetPreference: () => ({ mutate: setPref, isPending: false, error: null }),
}));

import { PreferenceControls } from "./PreferenceControls";

const pace = (over: object) => ({
  key: "pace",
  label: "Pace",
  value: "auto",
  source: "default",
  options: ["auto", "brisk", "standard", "unhurried"],
  global_value: null,
  inferred: "brisk",
  ...over,
});

describe("PreferenceControls", () => {
  beforeEach(() => setPref.mockClear());

  it("shows what adaptation currently chose when set to adapt", () => {
    rows = [pace({})];
    render(<PreferenceControls />);
    expect(screen.getByText(/Adapting to you: brisk/i)).toBeInTheDocument();
  });

  it("pins a value when chosen", () => {
    rows = [pace({})];
    render(<PreferenceControls />);
    fireEvent.change(screen.getByLabelText("Pace"), { target: { value: "unhurried" } });
    expect(setPref).toHaveBeenCalledWith({ key: "pace", value: "unhurried" });
  });

  it("per subject, shows the default until overridden, then offers to go back to it", () => {
    rows = [pace({ value: "brisk", source: "global", global_value: "brisk" })];
    const { rerender } = render(<PreferenceControls subjectId="s1" />);
    expect(screen.getByText(/Using your default/i)).toBeInTheDocument();

    rows = [pace({ value: "unhurried", source: "subject", global_value: "brisk" })];
    rerender(<PreferenceControls subjectId="s1" />);
    fireEvent.click(screen.getByRole("button", { name: /Use my default for Pace/i }));
    expect(setPref).toHaveBeenCalledWith({ key: "pace", value: "auto" });
  });
});
```

`ProfileSection.test.tsx`: mock `../../api/hooks` so `useProfile` returns one dimension `{ key: "pace", label: "Pace", observation: "…", value: {} }` and `usePreferences` returns `[pace({ value: "brisk", source: "global" })]`; assert `screen.getByText(/Your setting overrides this/)`. (Read `ProfileSection.tsx` for the exact hook names and dimension fields it renders; mock `useRefreshProfile`/`useResetDimension` too.)

- [ ] **Step 2: Run to verify failure** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/preferences src/components/dashboard/ProfileSection.test.tsx` → FAIL (module not found).

- [ ] **Step 3: Implement.**

`hooks.ts`:

```ts
/** The learner's explicit settings (S02), resolved for one subject or globally, each with
 * where it came from and what adaptation would choose. */
export function usePreferences(subjectId?: string) {
  return useQuery({
    queryKey: ["preferences", subjectId ?? "global"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/preferences", {
        params: { query: subjectId ? { subject_id: subjectId } : {} },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useSetPreference(subjectId?: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ key, value }: { key: string; value: string }) => {
      const { data, error } = await api.PUT("/api/v1/preferences/{key}", {
        params: { path: { key } },
        body: { value, subject_id: subjectId ?? null },
      });
      if (error) throw error;
      return data;
    },
    // Every subject's view and the plan's guidance can change with one setting.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["preferences"] });
      void queryClient.invalidateQueries({ queryKey: ["lesson-plan"] });
    },
  });
}
```

(Use the lesson-plan query key the file actually uses — grep `useLessonPlan`.)

`preferenceCopy.ts`:

```ts
/** Learner-facing words for preference values (S02). */
export const OPTION_LABELS: Record<string, string> = {
  auto: "Adapt to me",
  guided: "Guided",
  exploration: "Exploration",
  introductory: "Introductory",
  standard: "Standard",
  advanced: "Advanced",
  outline: "Outline",
  narrative: "Narrative",
  mnemonic: "Mnemonic",
  worked_examples: "Worked examples",
  fewer: "Fewer hints",
  some: "Some hints",
  more: "More hints",
  brisk: "Brisk",
  unhurried: "Unhurried",
};

export const optionLabel = (value: string) => OPTION_LABELS[value] ?? value;

/** Profile dimensions a setting overrides while it is pinned. */
export const OVERRIDDEN_DIMENSIONS: Record<string, string> = {
  help_seeking: "hints",
  persistence: "hints",
  pace: "pace",
  note_format: "note_format",
};
```

`PreferenceControls.tsx`:

```tsx
import { usePreferences, useSetPreference } from "../../api/hooks";
import { optionLabel } from "./preferenceCopy";

/** One control per setting (S02, V09). Without `subjectId` it edits the learner's defaults;
 * with one, that subject's overrides — each showing the default it would otherwise use. */
export function PreferenceControls({ subjectId }: { subjectId?: string }) {
  const { data } = usePreferences(subjectId);
  const set = useSetPreference(subjectId);
  if (!data) return null;
  return (
    <div className="flex flex-col gap-3">
      {data.map((pref) => {
        const overridden = subjectId !== undefined && pref.source === "subject";
        // The catalog default is always the first option ("auto", or "guided" for guidance).
        const defaultValue = pref.options[0];
        return (
          <div key={pref.key} className="flex flex-col gap-1">
            <label className="text-body flex items-center justify-between gap-3">
              <span>{pref.label}</span>
              <select
                aria-label={pref.label}
                className="select select-sm"
                value={pref.value}
                disabled={set.isPending}
                onChange={(e) => set.mutate({ key: pref.key, value: e.target.value })}
              >
                {pref.options.map((option) => (
                  <option key={option} value={option}>
                    {optionLabel(option)}
                  </option>
                ))}
              </select>
            </label>
            <p className="text-caption text-base-content/60">
              {subjectId !== undefined && !overridden && pref.global_value !== null
                ? `Using your default (${optionLabel(pref.global_value)}).`
                : pref.value === "auto" && pref.inferred
                  ? `Adapting to you: ${optionLabel(pref.inferred).toLowerCase()} for now.`
                  : null}
              {overridden && (
                <button
                  type="button"
                  className="link ml-1"
                  aria-label={`Use my default for ${pref.label}`}
                  onClick={() => set.mutate({ key: pref.key, value: defaultValue })}
                >
                  Use my default
                </button>
              )}
            </p>
          </div>
        );
      })}
    </div>
  );
}
```

(Note: "Use my default" sends the key's catalog default, which deletes the subject row — the server then resolves to the global value. For guidance the catalog default is `guided`, which also deletes.)

`Account.tsx`: a `<section>` titled "Preferences" between "Your data" and "Delete account", with a one-line explanation ("How Guru teaches you everywhere. You can override any of these for a single subject from its lesson plan.") and `<PreferenceControls />`.

`LessonPlanPanel.tsx`: replace the `GuidanceToggle` element (and `useSetGuidance` usage and imports) with a small "Settings for this subject" heading and `<PreferenceControls subjectId={subjectId} />`. Leave `GuidanceToggle.tsx` and its test in place only if something else imports it; otherwise delete both and ledger it.

`ProfileSection.tsx`: read `usePreferences()`; for a dimension whose `OVERRIDDEN_DIMENSIONS[d.key]` names a preference whose global `value !== "auto"`, render `<span className="text-caption text-base-content/60">Your setting overrides this</span>` under the observation.

- [ ] **Step 4: Run** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build`; prettier and eslint on changed files → green. `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/hooks.ts frontend/src/components/preferences frontend/src/pages/Account.tsx frontend/src/components/lessons/LessonPlanPanel.tsx frontend/src/components/dashboard/ProfileSection.tsx frontend/src/components/dashboard/ProfileSection.test.tsx
git status
git commit -m "feat(web): set how Guru teaches you, everywhere or per subject [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Include `GuidanceToggle` deletions if made.)

---

### Task 7: Tracker and CLAUDE.md [S02]

- [ ] **Step 1:** Tracker: move S02 to "Completed and consolidated work" (sorted by id) as **Implemented**: "Global defaults and subject overrides for guidance, explanation level, note format, hints and pace. An explicit setting pins its parameter in every mode (tutor, lessons, notes, detours); inferred values stay visible beside it and resettable on the Dashboard. Out of scope: explicit item type and challenge; wiring inferred pacing into the tutor." Evidence: `app/services/preferences.py`, `app/learning/preferences.py`, `tests/test_preferences.py`, `tests/test_preferences_api.py`. Add any deferred minors from the final review to the row.
- [ ] **Step 2:** CLAUDE.md Key Technical Decisions, after the account-deletion bullet:

```markdown
- **An explicit setting pins; inference adapts only what is left to it** (S02; V09) — five
  settings (guidance, explanation level, note format, hints, pace), global with subject
  overrides, resolved in one place (`app/services/preferences.py`) and applied when
  instructions are assembled, so a change reaches the next turn. Inferred values are still
  computed and shown beside the setting; only catalog strings ever reach a prompt.
```

- [ ] **Step 3:** `uv run poe check` → green; commit:

```bash
git add docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record explicit learner preferences [S02]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
