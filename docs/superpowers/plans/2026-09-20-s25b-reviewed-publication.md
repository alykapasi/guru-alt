# S25b Reviewed Publication — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A learner requests publication of their own subject, an administrator reviews exactly
what would ship, and approval materializes an immutable curated copy — while source-derived
material and stranger-visible slugs are both closed off.

**Architecture:** A frozen JSON snapshot is taken at request time and is the only thing approval
reads, so what the reviewer saw is what ships. Publication state lives in one new `publications`
table; the subject gains four columns describing its published life. Two privacy fixes ride along
because they need the same migration: subject slugs stop being globally unique (D8), and the
source-derived flag stops being carried by the client (D4).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, pytest;
React + TypeScript + Vite + TanStack Query, vitest, Playwright.

**Spec:** [docs/superpowers/specs/2026-09-19-s25-reviewed-publication-design.md](../specs/2026-09-19-s25-reviewed-publication-design.md)
(read it — this plan argues from it and does not restate its reasoning)

## Global Constraints

- Python 3.13; ruff line-length 100.
- Every commit ends green on: `uv run poe check`, `uv run poe format-check`,
  `uv run poe api-contract`, `uv run poe db-check`, plus `npm run build`, `npm run lint` and
  `npx vitest run` in `frontend/` for any commit touching the frontend.
- One tracker item id per commit subject, `[S25]`.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Stage only the task's files. Verify `git status` after staging and before committing: a red
  commit from an unstaged file happened in slice 2 (`67262bb`) and cost a fix-forward commit.
- Never reset, amend, rebase or force-push.
- **Do not touch `docs/guru-suggestions-tracker.md`** — it is the owner's uncommitted rewrite.
  Tracker rows that this slice would otherwise write go in the final report to the owner instead.
- `frontend/src/api/schema.d.ts` is generated. Regenerate it via the project's contract task; do
  not hand-edit.
- The frontend type gate is `npm run build`. `npx tsc --noEmit` checks nothing in this repo.

---

## File Structure

**Created:**

- `db/migrations/versions/0056_publication.py` — schema for the whole slice.
- `app/models/publication.py` — `Publication`, `PublicationStatus`, `CurriculumProposal`.
- `app/services/publication.py` — snapshot, request, cancel, approve, reject, withdraw. All the
  business logic; the routers stay thin.
- `app/schemas/publication.py` — request/response bodies.
- `app/api/v1/publications.py` — author routes (`/subjects/{id}/publications`, `/publications/*`).
- `tests/test_publication_slug_scope.py`, `tests/test_source_derived_flag.py`,
  `tests/test_publication_author.py`, `tests/test_publication_review.py`,
  `tests/test_publication_catalog.py`, `tests/test_publication_migration.py`.
- `frontend/src/api/publications.ts`, `frontend/src/components/PublishPanel.tsx`,
  `frontend/src/pages/AdminPublications.tsx` (+ their `.test.tsx`).

**Modified:**

- `app/models/knowledge.py` — `Subject` gains five columns and loses the global unique on `slug`.
- `app/services/knowledge.py` — `unique_subject_slug` helper; `create_subject_with_graph` scopes
  its de-duplication and takes `private_source_derived`; `list_subjects` gains the catalog rule.
- `app/api/v1/knowledge.py` — `/subjects/commit` requires `proposal_id`.
- `app/api/v1/onboarding.py` — `/onboarding/curriculum` records and returns `proposal_id`.
- `app/services/onboarding.py` — `generate_curriculum_for_onboarding` reports whether excerpts
  actually reached the model.
- `app/api/v1/sources.py`, `app/services/ingestion.py` — set the flag on upload/reassign.
- `app/api/v1/admin.py` — the review queue routes.
- `app/main.py` — register the publications router.
- `docs/RUNBOOK.md`, `docs/MASTERPLAN.md` — §, decision table.

---

## Task 1: Migration 0056 and the models

**Files:**

- Create: `db/migrations/versions/0056_publication.py`
- Create: `app/models/publication.py`
- Modify: `app/models/knowledge.py` (the `Subject` class)
- Test: `tests/test_publication_migration.py`

**Interfaces:**

- Produces: `Publication`, `PublicationStatus`, `CurriculumProposal` models; `Subject`'s new
  columns `private_source_derived`, `publication_id`, `superseded_by_id`, `withdrawn_at`,
  `withdrawn_reason`. Every later task consumes these.

**The circular foreign key.** `publications.source_subject_id` and `published_subject_id` point at
`subjects`, and `subjects.publication_id` points back at `publications`. Create `publications`
first (it references `subjects`, which already exists), *then* add `subjects.publication_id`. Do
not try to create both in one `create_table` pair.

**The downgrade hazard — read this before writing `downgrade()`.** After this migration two
learners may legitimately hold the same slug. Restoring the old global unique index would then
fail on real data, which turns the downgrade into a landmine that only goes off in production. The
downgrade must de-duplicate first, deterministically, before recreating the unique index. This is
the one part of the migration that is easy to get wrong and impossible to notice in an empty
database — `tests/test_publication_migration.py` pins it with data.

- [ ] **Step 1: Write the failing migration test**

`tests/test_publication_migration.py`, following the harness style of
`tests/test_password_retirement.py` (raw SQL seeding via `tests/migration_harness`):

```python
"""Migration 0056: publication schema, the D5 backfill, and per-owner slugs (S25b).

Seeding is raw SQL on purpose — see `tests/migration_harness`.
"""

import uuid

from tests.migration_harness import database_at, downgrade, upgrade

SCRATCH = "guru_migration_test"


async def test_the_backfill_marks_owned_subjects_and_leaves_curated_alone() -> None:
    """D5: nothing recorded whether a legacy subject came from uploads, so every owned one is
    assumed to have. Guessing the other way is what V03 forbids."""
    async with database_at("0055_retire_passwords") as connect:
        conn = await connect()
        try:
            learner = uuid.uuid4()
            owned, curated = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner, "author"
            )
            await conn.execute(
                "INSERT INTO subjects (id, slug, name, owner_learner_id) VALUES ($1,$2,$3,$4)",
                owned, "owned", "Owned", learner,
            )
            await conn.execute(
                "INSERT INTO subjects (id, slug, name) VALUES ($1, $2, $3)",
                curated, "curated", "Curated",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0056_publication")

        conn = await connect()
        try:
            assert await conn.fetchval(
                "SELECT private_source_derived FROM subjects WHERE id = $1", owned
            ) is True
            assert await conn.fetchval(
                "SELECT private_source_derived FROM subjects WHERE id = $1", curated
            ) is False
        finally:
            await conn.close()


async def test_two_learners_may_share_a_slug_and_one_learner_may_not() -> None:
    """The D8 constraint swap, asserted as behaviour rather than as index names."""
    async with database_at("0055_retire_passwords") as connect:
        await upgrade(SCRATCH, "0056_publication")
        conn = await connect()
        try:
            a, b = uuid.uuid4(), uuid.uuid4()
            for learner_id, handle in ((a, "a"), (b, "b")):
                await conn.execute(
                    "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, handle
                )
            await conn.execute(
                "INSERT INTO subjects (id, slug, name, owner_learner_id) VALUES ($1,$2,$3,$4)",
                uuid.uuid4(), "calculus", "Calculus", a,
            )
            # The point of D8: B's slug is not answerable from A's library.
            await conn.execute(
                "INSERT INTO subjects (id, slug, name, owner_learner_id) VALUES ($1,$2,$3,$4)",
                uuid.uuid4(), "calculus", "Calculus", b,
            )

            try:
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name, owner_learner_id) VALUES ($1,$2,$3,$4)",
                    uuid.uuid4(), "calculus", "Calculus Again", a,
                )
            except Exception as exc:  # asyncpg UniqueViolationError
                assert "uq_subjects_owner_slug" in str(exc)
            else:
                raise AssertionError("one learner may not hold the same slug twice")
        finally:
            await conn.close()


async def test_two_curated_subjects_may_not_share_a_slug() -> None:
    """The partial index. Without it the owner/slug constraint lets curated rows collide,
    because Postgres does not treat NULL owners as equal."""
    async with database_at("0055_retire_passwords") as connect:
        await upgrade(SCRATCH, "0056_publication")
        conn = await connect()
        try:
            await conn.execute(
                "INSERT INTO subjects (id, slug, name) VALUES ($1, $2, $3)",
                uuid.uuid4(), "shared", "Shared",
            )
            try:
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name) VALUES ($1, $2, $3)",
                    uuid.uuid4(), "shared", "Shared Too",
                )
            except Exception as exc:
                assert "uq_subjects_curated_slug" in str(exc)
            else:
                raise AssertionError("curated slugs stay unique among themselves")
        finally:
            await conn.close()


async def test_downgrade_deduplicates_slugs_instead_of_failing_on_them() -> None:
    """The hazard this migration creates: data the new constraints allow, the old one forbids.

    A downgrade that simply recreated the global unique index would fail here — in production,
    on real rows, at the worst moment. It renames instead, and the rename is deterministic.
    """
    async with database_at("0055_retire_passwords") as connect:
        await upgrade(SCRATCH, "0056_publication")
        conn = await connect()
        try:
            a, b = uuid.uuid4(), uuid.uuid4()
            for learner_id, handle in ((a, "a"), (b, "b")):
                await conn.execute(
                    "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, handle
                )
            for learner_id in (a, b):
                await conn.execute(
                    "INSERT INTO subjects (id, slug, name, owner_learner_id)"
                    " VALUES ($1, $2, $3, $4)",
                    uuid.uuid4(), "calculus", "Calculus", learner_id,
                )
        finally:
            await conn.close()

        await downgrade(SCRATCH, "0055_retire_passwords")

        conn = await connect()
        try:
            slugs = sorted(
                row["slug"] for row in await conn.fetch("SELECT slug FROM subjects")
            )
            assert slugs == ["calculus", "calculus_2"], slugs
            assert await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables"
                " WHERE table_name = 'publications'"
            ) == 0
        finally:
            await conn.close()
```

- [ ] **Step 2: Run it and watch it fail**

`uv run pytest tests/test_publication_migration.py -v` → FAIL, no revision `0056_publication`.

- [ ] **Step 3: Write the migration**

`db/migrations/versions/0056_publication.py`. Docstring states what the tests pin: the circular
FK order, the D5 backfill direction, and why the downgrade renames.

```python
revision: str = "0056_publication"
down_revision: str | Sequence[str] | None = "0055_retire_passwords"
```

`upgrade()`, in this order:

1. `op.add_column("subjects", sa.Column("private_source_derived", sa.Boolean(), nullable=False,
   server_default=sa.false()))`, then the D5 backfill
   `UPDATE subjects SET private_source_derived = true WHERE owner_learner_id IS NOT NULL`.
2. `superseded_by_id` (`sa.Uuid`, FK `subjects.id` `ondelete="SET NULL"`, nullable),
   `withdrawn_at` (`sa.DateTime(timezone=True)`, nullable), `withdrawn_reason` (`sa.Text`,
   nullable).
3. The slug swap. `0002` created the index as `op.f("ix_subjects_slug")`, so drop it the same way
   or the drop will not find it: `op.drop_index(op.f("ix_subjects_slug"), table_name="subjects")`;
   then `op.create_index(op.f("ix_subjects_slug"), "subjects", ["slug"])` (non-unique, for
   `list_subjects`' ordering); `op.create_unique_constraint("uq_subjects_owner_slug", "subjects",
   ["owner_learner_id", "slug"])`; and the partial index, which Alembic expresses as
   `op.create_index("uq_subjects_curated_slug", "subjects", ["slug"], unique=True,
   postgresql_where=sa.text("owner_learner_id IS NULL"))`.
4. `curriculum_proposals` — `id` (Uuid pk), `learner_id` (Uuid, FK `learners.id`
   `ondelete="CASCADE"`, not null, indexed), `grounded_in_sources` (Boolean, not null),
   `created_at`/`updated_at` (naive `sa.DateTime` with `server_default=sa.func.now()`, matching
   `TimestampMixin` — `poe db-check` catches the mismatch if these drift).
5. `publications` — per the spec's column list. `status` is a `sa.String()` (the codebase uses
   `StrEnum` in Python and plain strings in the database; see `ItemType`). `snapshot` and
   `excluded_item_ids` are `postgresql.JSONB`. Then
   `op.create_index("uq_publications_one_pending", "publications", ["source_subject_id"],
   unique=True, postgresql_where=sa.text("status = 'pending'"))`.
6. **Last**, because it closes the cycle: `subjects.publication_id` (Uuid, FK `publications.id`
   `ondelete="SET NULL"`, nullable).

`downgrade()`, reversing, with the de-duplication before the unique index is restored:

```python
    op.drop_column("subjects", "publication_id")
    op.drop_table("publications")
    op.drop_table("curriculum_proposals")

    # Before the global unique index comes back. This migration made slugs unique per owner, so
    # the database may legitimately hold two rows the old index forbids. Recreating it without
    # this renames nothing and simply fails — on real data, during a rollback, which is the
    # worst possible moment to discover it. Keeping the oldest occurrence unchanged makes the
    # rename deterministic and leaves the row most likely to be linked-to alone.
    op.execute(
        """
        UPDATE subjects SET slug = subjects.slug || '_' || ranked.position
        FROM (
            SELECT id, row_number() OVER (PARTITION BY slug ORDER BY created_at, id) AS position
            FROM subjects
        ) AS ranked
        WHERE subjects.id = ranked.id AND ranked.position > 1
        """
    )
    op.drop_index("uq_subjects_curated_slug", table_name="subjects")
    op.drop_constraint("uq_subjects_owner_slug", "subjects", type_="unique")
    op.drop_index(op.f("ix_subjects_slug"), table_name="subjects")
    op.create_index(op.f("ix_subjects_slug"), "subjects", ["slug"], unique=True)

    op.drop_column("subjects", "withdrawn_reason")
    op.drop_column("subjects", "withdrawn_at")
    op.drop_column("subjects", "superseded_by_id")
    op.drop_column("subjects", "private_source_derived")
```

> The rename can itself collide (a subject genuinely named `calculus_2` already existing). That is
> a downgrade of a downgrade-hazard; leave it, and say so in the docstring rather than building a
> retry loop nobody will ever exercise.

- [ ] **Step 4: Write the models**

`app/models/publication.py` — `PublicationStatus(StrEnum)` with `PENDING`/`APPROVED`/`REJECTED`/
`CANCELLED`; `Publication` and `CurriculumProposal` mirroring the migration exactly. The
docstrings carry the *why*: SET NULL plus text handles so the record outlives the accounts, as
`impersonations` does; CASCADE on `curriculum_proposals.learner_id` because it is scaffolding for
one commit, not an audit record.

`app/models/knowledge.py` — on `Subject`, `slug` becomes `mapped_column(index=True)` (the
`unique=True` goes), and `__table_args__` gains
`UniqueConstraint("owner_learner_id", "slug", name="uq_subjects_owner_slug")`. Add the five new
columns. **Update the class docstring**: it currently narrates the pre-S25a world and now needs
the D8 sentence, because the docstring is what the next reader trusts about slug scope.

> The partial unique index is *not* declared on the model — SQLAlchemy would need an `Index` with
> `postgresql_where`, and `poe db-check` compares what autogenerate sees. Declare it as
> `Index("uq_subjects_curated_slug", "slug", unique=True, postgresql_where=text("owner_learner_id
> IS NULL"))` inside `__table_args__` so the model and the migration agree. **Run `poe db-check`
> and believe it over this plan** — if autogenerate reports drift, the model is what to fix.

- [ ] **Step 5: Run the tests and the schema gate**

```bash
uv run pytest tests/test_publication_migration.py -v
uv run poe db-check
uv run poe check
```

All must pass. `db-check` clean is the one that proves the models and the migration agree.

- [ ] **Step 6: Commit**

```bash
git add db/migrations/versions/0056_publication.py app/models/publication.py \
        app/models/knowledge.py tests/test_publication_migration.py
git status   # nothing of this task's left unstaged
git commit
```

Subject: `feat(publication): schema for reviewed publication [S25]`

---

## Task 2: Per-owner slug de-duplication (D8)

**Files:**

- Modify: `app/services/knowledge.py` (add `unique_subject_slug`; use it in
  `create_subject_with_graph` at the block currently at lines 207-215)
- Test: `tests/test_publication_slug_scope.py`

**Interfaces:**

- Consumes: Task 1's constraints.
- Produces: `async def unique_subject_slug(session: AsyncSession, name: str, *,
  owner_learner_id: uuid.UUID | None) -> str` — Task 5's approval uses it for curated slugs.

- [ ] **Step 1: Write the failing test**

```python
"""Subject slugs stop reporting on other learners' libraries (S25b D8).

The old de-duplication counted every slug in the table, so the `_2` suffix answered "does
somebody have a subject by this name?" for any name worth trying — a probe anyone could repeat
with create, read, delete.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.services import knowledge as svc


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"slug-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_a_strangers_subject_does_not_suffix_mine(db_session: AsyncSession) -> None:
    """The leak, stated as the behaviour that closes it."""
    a, b = await _learner(db_session), await _learner(db_session)
    await svc.create_subject_with_graph(
        db_session, subject_name="Calculus", subject_description=None,
        topics_data=[], source_ids=None, learner_id=a.id,
    )

    result = await svc.create_subject_with_graph(
        db_session, subject_name="Calculus", subject_description=None,
        topics_data=[], source_ids=None, learner_id=b.id,
    )

    assert result.subject.slug == "calculus", (
        "a suffix here tells B that somebody already has a subject called Calculus"
    )


async def test_my_own_second_subject_is_still_suffixed(db_session: AsyncSession) -> None:
    """Per-owner uniqueness is still uniqueness — this is the half that must not regress."""
    a = await _learner(db_session)
    for _ in range(2):
        result = await svc.create_subject_with_graph(
            db_session, subject_name="Calculus", subject_description=None,
            topics_data=[], source_ids=None, learner_id=a.id,
        )
    assert result.subject.slug == "calculus_2"


async def test_a_curated_slug_dedups_against_curated_only(db_session: AsyncSession) -> None:
    """Approval (Task 5) picks curated slugs through this, so it must not see owned rows."""
    a = await _learner(db_session)
    await svc.create_subject_with_graph(
        db_session, subject_name="Calculus", subject_description=None,
        topics_data=[], source_ids=None, learner_id=a.id,
    )

    slug = await svc.unique_subject_slug(db_session, "Calculus", owner_learner_id=None)

    assert slug == "calculus", "a learner's private subject must not push the curated slug along"
```

- [ ] **Step 2: Run it and watch the first and third fail**

`uv run pytest tests/test_publication_slug_scope.py -v` → the first fails with
`calculus_2 != calculus`, the third with `AttributeError: unique_subject_slug`. The second passes
already; that is expected and is why it is here.

- [ ] **Step 3: Implement**

In `app/services/knowledge.py`, replace the lines currently at 207-215:

```python
async def unique_subject_slug(
    session: AsyncSession, name: str, *, owner_learner_id: uuid.UUID | None
) -> str:
    """A slug free among the subjects sharing this owner (S25b D8).

    Scoped, not global. A global scan made the suffix an existence oracle: name a subject what
    a stranger privately named theirs and the `_2` you got back answered a question about their
    library. It also loaded every slug in the table to create one row.

    ``owner_learner_id=None`` scopes to curated subjects, which are the shared library and
    genuinely are unique among themselves.
    """
    owner = (
        Subject.owner_learner_id.is_(None)
        if owner_learner_id is None
        # `== None` would compile to `= NULL`, which is never true — the filter would match
        # nothing and every name would look free. `.is_(None)` above is not a style choice.
        else Subject.owner_learner_id == owner_learner_id
    )
    taken = {slug for (slug,) in (await session.execute(select(Subject.slug).where(owner))).all()}
    base = _slugify(name)
    slug, counter = base, 2
    while slug in taken:
        slug = f"{base}_{counter}"
        counter += 1
    return slug
```

and call it: `subject_slug = await unique_subject_slug(session, subject_name,
owner_learner_id=learner_id)`.

Fix the now-wrong docstring in `create_subject_with_graph` — it says "Subject slug: globally
unique (dedups across all subjects)", which is exactly the claim this task deletes.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/test_publication_slug_scope.py tests/test_knowledge.py -v
uv run poe check
```

`tests/test_knowledge.py` is in scope because it exercises subject creation and may assert on
suffixes that were only correct under global scoping.

- [ ] **Step 5: Commit**

Subject: `fix(knowledge): scope slug de-duplication to the owner [S25]`

---

## Task 3: The source-derived flag, recorded server-side (D4)

**Files:**

- Modify: `app/services/onboarding.py` (`generate_curriculum_for_onboarding`),
  `app/api/v1/onboarding.py` (`CurriculumResponse`, the route),
  `app/api/v1/knowledge.py` (`commit_subject`), `app/schemas/knowledge.py`
  (`SubjectCommitRequest`), `app/services/knowledge.py` (`create_subject_with_graph`),
  `app/api/v1/sources.py` and `app/services/ingestion.py` (upload / reassign)
- Test: `tests/test_source_derived_flag.py`

**Interfaces:**

- Consumes: `CurriculumProposal` model (Task 1).
- Produces: `create_subject_with_graph(..., private_source_derived: bool)`;
  `mark_source_derived(session, subject_id)` in `app/services/knowledge.py`, used by every
  trigger.

**The rule this task exists to enforce:** the flag is a latch, set from what the *server*
observed, never from a request body. Re-setting an already-set flag is a no-op, not an error.

- [ ] **Step 1: Write the failing tests**

`tests/test_source_derived_flag.py` covers each trigger and, most importantly, the omission
attack. Through the real API with `api_client`:

```python
"""A subject built from private uploads can never be published (S25b D4).

The interesting test is `test_a_client_cannot_opt_out_by_omission`: the flag used to be a bit
the browser carried from generation to commit, which is not a control — a bit you hand the
client is a bit the client can drop.
"""
```

Cases, each its own test:

1. `test_a_generation_that_used_excerpts_flags_the_subject` — generate with `source_ids` on a
   learner who has an ingested source, commit with the returned `proposal_id`, assert
   `private_source_derived is True`.
2. `test_a_generation_that_used_no_excerpts_does_not_flag` — generate with `source_ids=None`,
   commit, assert `False`. Without this the flag creeps until nothing is publishable.
3. `test_source_ids_passed_only_at_commit_flag_the_subject` — the independent trigger (2).
4. `test_a_client_cannot_opt_out_by_omission` — generate *with* excerpts, then commit with the
   returned `proposal_id` but `source_ids=None` and nothing in the body about grounding. Assert
   `True`. **This is the test the task exists for.**
5. `test_a_commit_without_a_proposal_id_is_refused` — 422 from FastAPI's own validation, because
   the field is required.
6. `test_another_learners_proposal_id_is_a_404` — the ordinary not-visible answer, not a 403.
7. `test_uploading_a_source_into_a_subject_flags_it` — trigger (3).
8. `test_reassigning_a_source_into_a_subject_flags_it` — trigger (3), the later-move half.
9. `test_the_flag_is_a_latch` — flag a subject, run a trigger that would set it again, assert no
   error and still `True`.

> `generate_curriculum_for_onboarding` calls `retrieval.retrieve`, which needs embedded chunks.
> Follow the existing fixtures in `tests/test_onboarding*.py` for seeding an ingested source
> rather than inventing one; `tests/embedding.py` holds the fake embedding space.

- [ ] **Step 2: Run them and watch them fail**

`uv run pytest tests/test_source_derived_flag.py -v` — all fail; 5 and 6 fail because
`proposal_id` does not exist yet.

- [ ] **Step 3: Record grounding at generation**

`app/services/onboarding.py` — `generate_curriculum_for_onboarding` returns
`tuple[CurriculumProposal | None, bool]`, the bool being `materials is not None`, i.e. whether
excerpts were actually retrieved and sent. Passing `source_ids` that retrieved nothing is *not*
grounding: no source text reached the model, and flagging it would make honest subjects
unpublishable for no privacy gain.

`app/api/v1/onboarding.py` — after a successful generation, write the row and return its id:

```python
    proposal, grounded = await onboarding.generate_curriculum_for_onboarding(...)
    ...
    record = CurriculumProposal(learner_id=learner.id, grounded_in_sources=grounded)
    session.add(record)
    await session.commit()
```

`CurriculumResponse` gains `proposal_id: uuid.UUID`.

- [ ] **Step 4: Require it at commit**

`SubjectCommitRequest` gains `proposal_id: uuid.UUID` (required — no default). In
`commit_subject`, before anything else:

```python
    record = await session.get(CurriculumProposal, request.proposal_id)
    if record is None or record.learner_id != learner.id:
        # The same 404 for "no such proposal" and "somebody else's": a caller who could tell
        # them apart could probe for other learners' activity, which is the shape of leak this
        # slice exists to close.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such proposal")
```

and pass `private_source_derived=record.grounded_in_sources or bool(request.source_ids)` into
`create_subject_with_graph`, which sets it on the `Subject` it builds.

- [ ] **Step 5: The upload and reassignment triggers**

Add to `app/services/knowledge.py`:

```python
async def mark_source_derived(session: AsyncSession, subject_id: uuid.UUID | None) -> None:
    """Latch the flag (S25b D4). A no-op when already set, and when there is no subject."""
```

Call it wherever a source acquires a `subject_id`: the upload route in `app/api/v1/sources.py`
(which takes `subject_id` as a form field), the source-update path in the same module, and
`create_subject_with_graph`'s reassignment block. Each call is in the same transaction as the
assignment — a flag set in a later transaction is a window where the subject is publishable.

- [ ] **Step 6: Run everything**

```bash
uv run pytest tests/test_source_derived_flag.py -v
uv run poe check && uv run poe api-contract
```

`api-contract` will report drift from the two changed request/response bodies — regenerate, and
read the diff to confirm it is only `proposal_id`.

- [ ] **Step 7: Fix the frontend caller**

`frontend/src/api/onboarding.ts` — `CurriculumProposal` gains `proposal_id`, and
`SubjectCommitPayload` carries it through to `/subjects/commit`. `npm run build` is the gate that
proves the type change is threaded; the commit is red without it.

- [ ] **Step 8: Commit**

Subject: `feat(publication): record source grounding server-side [S25]`

---

## Task 4: Requesting publication — snapshot and cancel

**Files:**

- Create: `app/services/publication.py`, `app/schemas/publication.py`,
  `app/api/v1/publications.py`
- Modify: `app/main.py` (register the router)
- Test: `tests/test_publication_author.py`

**Interfaces:**

- Produces: `snapshot_of(session, subject) -> dict`, `request_publication(session, subject,
  author, note) -> Publication`, `cancel(session, publication, author) -> Publication`.
  Task 5 consumes the snapshot shape.

**Snapshot shape**, exactly as the spec fixes it — keyed by source id so the reviewer can exclude
items:

```json
{"subject": {"name": "...", "description": "..."},
 "topics": [{"id": "...", "slug": "...", "name": "...", "description": "..."}],
 "kcs": [{"id": "...", "topic_id": "...", "slug": "...", "name": "...", "description": "..."}],
 "edges": [{"prereq_kc_id": "...", "kc_id": "...", "weight": 1.0}],
 "items": [{"id": "...", "item_type": "...", "stem": "...", "answer_key": {},
            "difficulty": 0.0, "rubric_id": null, "origin": "...",
            "kc_weights": [{"kc_id": "...", "weight": 1.0}]}],
 "rubrics": [{"id": "...", "kc_id": "...", "name": "...", "criteria": {}}]}
```

**What the snapshot must exclude, and why each is a separate test:** evidence, mastery, FSRS
state, lesson plans, notes, content blocks, sources, chunks, conversations, memories, and edges
whose other end is a KC in another subject. Items are restricted to
`Item.owner_learner_id == author.id` *and* tagged only to this subject's KCs — an item the author
owns but which also tags a KC elsewhere carries that other subject's structure with it.

- [ ] **Step 1: Write the failing tests**

`tests/test_publication_author.py`, through the real API with three learners (author A,
admin R, learner C — `tests/conftest.py` has the admin fixture at line 186):

1. `test_the_owner_can_request_and_the_snapshot_is_frozen` — 201, status `pending`; then A edits
   the subject and the stored snapshot is unchanged.
2. `test_a_stranger_gets_the_same_404_as_a_random_id` — C requesting on A's subject, and C
   requesting on `uuid4()`, produce byte-identical responses.
3. `test_the_snapshot_carries_only_this_authors_items_on_this_subjects_kcs` — seed decoys: another
   learner's item on the same KC, and an item of A's on a KC in a different subject with the same
   name. Neither appears.
4. `test_the_snapshot_carries_no_private_material` — seed a note, a content block, a source, a
   conversation and evidence on the subject; assert none of their ids appear anywhere in
   `json.dumps(snapshot)`. One assertion over the serialized blob catches a field added later that
   a field-by-field test would miss.
5. `test_cross_subject_edges_do_not_travel` — an edge from a KC here to a KC in A's other subject
   is absent.
6. `test_a_source_derived_subject_is_refused` — 422 with the spec's exact copy: `"This subject was
   built from your uploaded material, which stays private."`
7. `test_a_second_pending_request_is_a_409` — and the partial unique index is what enforces it, so
   assert the API's 409 rather than an `IntegrityError` reaching the caller.
8. `test_a_subject_with_no_kcs_is_refused` — 422.
9. `test_the_author_can_cancel_and_only_while_pending` — cancel → `cancelled`; cancelling an
   already-cancelled one is refused; C cancelling A's is a 404.
10. `test_the_history_lists_requests_with_status_and_note`.

- [ ] **Step 2: Run them and watch them fail** (`404`, no router registered)

- [ ] **Step 3: Implement the service, then the schemas, then the routes**

Business logic lives in `app/services/publication.py`; the router stays thin, as
`app/api/v1/admin.py` does. Reuse the existing ownership gate —
`knowledge.is_writable_by` plus the `NotVisible` → 404 handler in `app/main.py` — rather than
writing a second one.

- [ ] **Step 4: Run, then `poe check`, `poe api-contract`**

- [ ] **Step 5: Commit** — `feat(publication): authors can request publication [S25]`

---

## Task 5: Review and approval

**Files:**

- Modify: `app/api/v1/admin.py`, `app/services/publication.py`, `app/schemas/publication.py`
- Test: `tests/test_publication_review.py`

**Interfaces:**

- Consumes: the snapshot shape (Task 4), `unique_subject_slug` (Task 2).
- Produces: `approve(session, publication, reviewer, excluded_item_ids, note) -> Subject`.

**Approval materializes the snapshot, not the subject.** Read every row from the frozen JSON. If
approval re-read the live subject, the reviewer's decision would not be about what ships — which
is the entire point of D3, and the easiest thing in this slice to get quietly wrong.

- [ ] **Step 1: Write the failing tests**

1. `test_approval_materializes_the_snapshot_not_the_current_subject` — A edits the subject
   heavily after requesting; the published copy matches the snapshot. **The load-bearing test.**
2. `test_the_published_subject_is_curated_and_owned_by_nobody` — `owner_learner_id is None`,
   `publication_id` set.
3. `test_excluded_items_are_absent_and_the_rest_are_curated` — `visibility == CURATED`,
   `owner_learner_id is None`, `origin` and `author_learner_id` preserved.
4. `test_kc_links_are_remapped_to_the_new_kcs` — no `ItemKC` on the copy points at an original KC.
5. `test_the_published_slug_does_not_collide_with_a_curated_one`.
6. `test_rejection_requires_a_note_of_at_least_eight_characters` — 422 below that.
7. `test_a_non_admin_and_an_impersonated_admin_are_both_403` — parametrized over each admin route.
8. `test_approval_is_one_transaction` — force a failure partway (an item whose rubric id is
   missing) and assert no partial subject survives.

- [ ] **Step 2: Run them and watch them fail**

- [ ] **Step 3: Implement**

Create topics and KCs through the ordinary creation paths so concept identity follows S24's
rules. Copy items and rubrics with `visibility=CURATED`, `owner_learner_id=None`, `origin` and
`author_learner_id` preserved, remapping `ItemKC.kc_id` through an
`{old_kc_id: new_kc_id}` map built while creating the KCs. Rubrics first — items reference them.

- [ ] **Step 4: Run, gates, commit.** Subject:
      `feat(publication): administrators review and approve [S25]`

---

## Task 6: Supersede, withdraw, and the catalog rule

**Files:**

- Modify: `app/services/knowledge.py` (`list_subjects`), `app/services/publication.py`,
  `app/api/v1/admin.py`
- Test: `tests/test_publication_catalog.py`

**The catalog rule** — `list_subjects(learner)` returns the learner's own subjects; curated
subjects that are neither superseded nor withdrawn; and superseded or withdrawn curated subjects
on which *this* learner has a lesson plan. `is_visible_to` is unchanged: curated stays reachable
by id, deliberately, because it was reviewed as shareable.

- [ ] **Step 1: Write the failing tests**

1. `test_republishing_supersedes_the_previous_version`.
2. `test_a_learner_with_a_plan_on_v1_still_sees_it` / `test_a_new_learner_does_not`.
3. `test_withdrawal_unlists_with_the_same_rule`.
4. `test_withdrawal_requires_a_reason` and `test_withdraw_works_only_on_published_subjects`.
5. `test_an_unlisted_subject_is_still_reachable_by_id` — pins the deliberate half of D7.

- [ ] **Step 2-4: Run, implement, gates, commit.** Subject:
      `feat(publication): supersede and withdraw [S25]`

> `list_subjects` currently takes `learner_id: uuid.UUID | None`. The lesson-plan clause needs a
> join to `lesson_plans`; keep the `None` branch (used by fixtures and ops paths) returning the
> unfiltered list rather than growing a third meaning.

---

## Task 7: Frontend

**Files:**

- Create: `frontend/src/api/publications.ts`, `frontend/src/components/PublishPanel.tsx`,
  `frontend/src/pages/AdminPublications.tsx` and their tests
- Modify: `frontend/src/pages/Admin.tsx` (queue link), the subject page (Publish action)

**Copy rules:** name the control for what happens (`Publish`, `Request publication`), and when it
is unavailable say why in the learner's terms — the source-derived reason is the spec's sentence,
verbatim. Two controls must never share a label; slice 2 hit this with two "Suspend" buttons and
settled it by following the existing `View as` → `Start viewing` convention.

- [ ] **Step 1: Author panel** — status, review note, the unavailable reason. Test with vitest.
- [ ] **Step 2: Admin queue** — graph outline, items with keys, per-item exclusion, approve/reject
      with a note, withdraw on published subjects.
- [ ] **Step 3: `npm run build && npm run lint && npx vitest run`**
- [ ] **Step 4: Commit** — `feat(publication): publish and review in the browser [S25]`

---

## Task 8: Documentation

**Files:**

- Modify: `docs/RUNBOOK.md` (a §12 for the review queue), `docs/MASTERPLAN.md` (decision table),
  `CLAUDE.md` if any claim it makes about visibility is now stale

- [ ] **Step 1:** Write §12 — how to find pending publications, what the reviewer is actually
      deciding, what approval does that cannot be undone (the copy is immutable; withdrawal
      unlists but does not unshare), and the D4 residual stated in the spec, because the reviewer
      is the control that residual falls back on and they should know it.
- [ ] **Step 2:** Verify every `poe` task and path named in the docs exists. Slice 2 caught two
      false claims this way.
- [ ] **Step 3: Commit** — `docs: how to review a publication [S25]`

---

## Self-Review Notes

**Spec coverage:** D1 Task 4 · D2 Task 4 · D3 Task 5 step 1 test 1 · D4 Task 3 · D5 Task 1 ·
D6 Task 4 (author not exposed in learner-facing reads) · D7 Task 6 · D8 Tasks 1-2. Migration,
flow, API, catalog and frontend sections each map to a task. The spec's Verification section is
distributed across the per-task tests; no bullet is unclaimed.

**Known risks, in the order they are likely to bite:**

1. The downgrade de-duplication (Task 1) is the only irreversible-looking step. Its test seeds the
   colliding data on purpose.
2. Approval reading the live subject instead of the snapshot (Task 5) would pass every test that
   does not edit the subject between request and approval. Test 1 edits it.
3. `poe db-check` and the partial indexes are the most likely source of churn. Believe the tool.
4. `tests/test_knowledge.py` may assert slug suffixes that only held under global scoping (Task 2).

**Two suite failures predate this branch** and are not this slice's to fix:
`tests/test_retrieval.py::test_keyword_match_boosts_ranking` (lead: `tests/embedding.py:12`
computes `FAKE_SPACE` once at import from settings that `tests/migration_harness.py:88,97` later
invalidate). If one of them fires during this slice, do not chase it here.
