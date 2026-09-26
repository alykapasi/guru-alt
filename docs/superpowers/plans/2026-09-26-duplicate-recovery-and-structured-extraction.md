# Duplicate Recovery and Structured Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A text duplicate is re-ingested from its own file whenever its original stops standing in; structured blocks survive chunking whole or split cleanly; machine-read passages say so to the tutor and the learner.

**Architecture:** `sources.duplicate_of_id` (FK, SET NULL) makes the duplicate relation queryable; one invariant defines a stranded duplicate, released at the two known causes (curriculum reassignment, reindex scope repair) and by the reconcile sweep. A new `app/rag/structure.py` segments text into prose/code/table/math before windowing. `reading_note` derives a note from provenance facts, rendered by the grounding policy, lesson prompts and the citation pane.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, pytest; React + TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-09-26-duplicate-recovery-and-structured-extraction-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract` (after `uv run poe api-types`, stage `frontend/src/api/schema.d.ts` — the contract check compares against the index).
- Frontend changes also green on `cd frontend && npm run build` and `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`.
- New migration → `uv run python -m tests.testdb`.
- In async tests never call `session.expire_all()` before touching test objects; re-read with `execution_options(populate_existing=True)` / `session.get(..., populate_existing=True)`, and read ids into locals before any code path that may roll back.
- One tracker id per commit subject: `[S77]` or `[S27]` as each task states.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls; tests use `fake_llm_client`.

## Review Focus

1. A duplicate whose original is re-processed and *fails* (FAILED, but its old chunks still current) — the duplicate is released by the sweep because the original is not DONE; it then re-ingests on its own (Task 2 test).
2. Releasing the same duplicate twice (reassignment then sweep before the worker runs) — second release is a no-op because the source is no longer DONE (Task 2 test).
3. A document that is one enormous table (longer than the ingest chunk budget allows at 4000 per part) — still bounded by `ingest_max_chunks` as before; parts never exceed `BLOCK_CAP` (Task 4 test on part sizes).
4. A code fence inside a table cell or a `$$` inside a code block — the block that opens first wins; nothing nests (Task 4 test: `$$` inside a fence stays code).
5. A passage with a reading note that is also cited — the note is not part of the citation marker and does not shift `[N]` numbering (Task 5 test).

---

### Task 1: `duplicate_of_id` column [S77]

**Files:**
- Create: `db/migrations/versions/0065_source_duplicate_of.py`
- Modify: `app/models/source.py` (class `Source`), `app/rag/pipeline.py` (twin branch in `run`), `app/schemas/source.py` (`SourceRead`)
- Test: `tests/test_text_dedup.py`, `tests/test_simhash.py`, `tests/test_migrations_with_data.py`

**Interfaces:**
- Produces: `Source.duplicate_of_id: uuid.UUID | None`; `SourceRead.duplicate_of_id: uuid.UUID | None`.

- [ ] **Step 1: Change the existing assertions to the column (RED).** In `tests/test_text_dedup.py` replace `assert second.meta["duplicate_of"] == str(first.id)` with `assert second.duplicate_of_id == first.id` and `assert "duplicate_of" not in other.meta` with `assert other.duplicate_of_id is None`. In `tests/test_simhash.py` replace `assert "duplicate_of" not in scan.meta` with `assert scan.duplicate_of_id is None`. Append to `tests/test_migrations_with_data.py`:

```python
async def test_duplicates_recorded_in_meta_get_the_column_backfilled() -> None:
    """0065 (S77): the relation moves from meta into a foreign key. An original that still
    exists is linked; one that is gone backfills to NULL, which is what lets the sweep find it."""
    async with database_at("0064_chunk_versions") as connect:
        conn = await connect()
        try:
            learner_id = uuid.uuid4()
            original, linked, orphan = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "reader"
            )
            for sid, meta in (
                (original, "{}"),
                (linked, f'{{"duplicate_of": "{original}"}}'),
                (orphan, f'{{"duplicate_of": "{uuid.uuid4()}"}}'),
            ):
                await conn.execute(
                    "INSERT INTO sources (id, learner_id, kind, origin, status, meta, attempts) "
                    "VALUES ($1, $2, 'file', 'x.txt', 'done', $3::jsonb, 0)",
                    sid,
                    learner_id,
                    meta,
                )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0065_source_duplicate_of")

        conn = await connect()
        try:
            rows = {
                r["id"]: r["duplicate_of_id"]
                for r in await conn.fetch("SELECT id, duplicate_of_id FROM sources")
            }
            assert rows[linked] == original
            assert rows[orphan] is None
            assert rows[original] is None
        finally:
            await conn.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_text_dedup.py tests/test_simhash.py -q`
Expected: FAIL — `AttributeError: 'Source' object has no attribute 'duplicate_of_id'`.

- [ ] **Step 3: Migration** `db/migrations/versions/0065_source_duplicate_of.py`:

```python
"""sources.duplicate_of_id: the text-duplicate relation as a foreign key (S77).

It lived in ``meta.duplicate_of``, where nothing noticed when the original went away. As a
foreign key with ON DELETE SET NULL, a deleted original leaves a NULL the recovery sweep can
find. Backfilled from meta where the named original still exists; an original that is already
gone backfills to NULL, which is exactly the stranded state the sweep recovers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_source_duplicate_of"
down_revision: str | Sequence[str] | None = "0064_chunk_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "duplicate_of_id",
            sa.Uuid(),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_sources_duplicate_of_id", "sources", ["duplicate_of_id"])
    op.execute(
        """
        UPDATE sources AS d SET duplicate_of_id = o.id
          FROM sources AS o
         WHERE d.meta ? 'duplicate_of'
           AND o.id::text = d.meta->>'duplicate_of'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_sources_duplicate_of_id", table_name="sources")
    op.drop_column("sources", "duplicate_of_id")
```

Run `uv run python -m tests.testdb`.

- [ ] **Step 4: Model, pipeline, schema.**
  - `app/models/source.py`, class `Source`, after `simhash`:

```python
    # The source this one is a text duplicate of (S77): same learner, same scope, same text,
    # so it was not chunked and the original answers for it. SET NULL on delete, so an original
    # that goes away leaves a mark the recovery sweep can find rather than a dangling id in meta.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), default=None, index=True
    )
```

  - `app/rag/pipeline.py`, twin branch in `run`: replace `source.meta = {**source.meta, "duplicate_of": str(twin.id)}` with `source.duplicate_of_id = twin.id`.
  - `app/schemas/source.py`, `SourceRead`: add `duplicate_of_id: uuid.UUID | None = None` after `topic_id`.

- [ ] **Step 5: Run tests and the gate**

Run: `uv run pytest tests/test_text_dedup.py tests/test_simhash.py tests/test_migrations_with_data.py -q` → PASS.
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 6: Commit**

```bash
git add db/migrations/versions/0065_source_duplicate_of.py app/models/source.py app/rag/pipeline.py app/schemas/source.py frontend/src/api/schema.d.ts tests/test_text_dedup.py tests/test_simhash.py tests/test_migrations_with_data.py
git status
git commit -m "feat(sources): record the text-duplicate relation as a foreign key [S77]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Stranded duplicates are found and released [S77]

**Files:**
- Modify: `app/services/ingestion.py` (`release_duplicates`, `stranded_duplicates`, `reconcile_stranded`, `ReconcileReport`), `app/workers/reconcile.py`
- Test: `tests/test_duplicate_recovery.py` (create)

**Interfaces:**
- Consumes: `Source.duplicate_of_id` (Task 1); `Chunk.superseded_at` (earlier slice).
- Produces:
  ```python
  async def release_duplicates(session: AsyncSession, source_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]
  async def stranded_duplicates(session: AsyncSession, *, learner_id: uuid.UUID | None = None) -> list[uuid.UUID]
  ReconcileReport.recovered: int
  ```

- [ ] **Step 1: Write the failing tests** — `tests/test_duplicate_recovery.py`:

```python
"""A text duplicate never silently grounds nothing (S77)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.services import ingestion
from app.storage import InMemoryBlobStore

BRITISH = b"The colour of the neighbouring fibre was analysed in the laboratory."
AMERICAN = b"The color of the neighboring fiber was analyzed in the laboratory."


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def _pair(session: AsyncSession) -> tuple[InMemoryBlobStore, uuid.UUID, uuid.UUID]:
    """An original and its text duplicate, both DONE. Returns (store, original_id, dup_id)."""
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    ids = []
    for data, name in ((BRITISH, "uk.txt"), (AMERICAN, "us.txt")):
        source = await ingestion.create_source(
            session,
            store,
            learner_id=learner.id,
            kind=SourceKind.FILE,
            origin=name,
            content_type="text/plain",
            data=data,
        )
        await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
        ids.append(source.id)
    return store, ids[0], ids[1]


async def _get(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id, populate_existing=True)
    assert source is not None
    return source


async def _current_chunks(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.source_id == source_id, Chunk.superseded_at.is_(None))
        )
    ) or 0


async def test_a_healthy_duplicate_is_not_stranded(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    assert (await _get(db_session, dup)).duplicate_of_id == original
    assert dup not in await ingestion.stranded_duplicates(db_session)


async def test_a_duplicate_whose_original_was_deleted_is_stranded(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_a_duplicate_whose_original_moved_scope_is_stranded(
    db_session: AsyncSession,
) -> None:
    from app.models.knowledge import Subject

    _, original, dup = await _pair(db_session)
    moved = await _get(db_session, original)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S", owner_learner_id=moved.learner_id)
    db_session.add(subject)
    await db_session.flush()
    moved.subject_id = subject.id
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_a_duplicate_whose_original_failed_is_stranded(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    (await _get(db_session, original)).status = SourceStatus.FAILED
    await db_session.commit()

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_release_puts_it_back_in_the_queue_and_it_reingests_from_its_own_file(
    db_session: AsyncSession,
) -> None:
    store, original, dup = await _pair(db_session)
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()

    released = await ingestion.release_duplicates(db_session, [dup])
    await db_session.commit()
    released_again = await ingestion.release_duplicates(db_session, [dup])

    assert released == [dup]
    assert released_again == [], "no longer DONE, so a second release does nothing"
    source = await _get(db_session, dup)
    assert source.status == SourceStatus.PENDING and source.duplicate_of_id is None
    await ingestion.ingest_source(db_session, store, fake_llm_client(), dup)
    assert await _current_chunks(db_session, dup) >= 1


async def test_the_reconcile_sweep_recovers_stranded_duplicates(db_session: AsyncSession) -> None:
    _, original, dup = await _pair(db_session)
    await db_session.delete(await _get(db_session, original))
    await db_session.commit()
    queue = _Queue()

    report = await ingestion.reconcile_stranded(db_session, queue, settings=Settings())

    assert report.recovered == 1
    assert dup in queue.enqueued
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_duplicate_recovery.py -q`
Expected: FAIL — `AttributeError: module 'app.services.ingestion' has no attribute 'stranded_duplicates'`.

- [ ] **Step 3: Implement** in `app/services/ingestion.py` (import `Chunk` from `app.models.source`, `aliased` from `sqlalchemy.orm`, `Sequence` from `collections.abc`, `exists` from `sqlalchemy` as needed):

```python
def _chunkless_done():
    """A DONE source with no current chunks — by construction a text duplicate (S77)."""
    has_chunks = (
        select(Chunk.id)
        .where(Chunk.source_id == Source.id, Chunk.superseded_at.is_(None))
        .exists()
    )
    return and_(Source.status == SourceStatus.DONE, ~has_chunks)


async def stranded_duplicates(
    session: AsyncSession, *, learner_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Duplicates whose original no longer stands in for them (S77).

    Healthy only while the original exists, is DONE, shares the learner, subject and topic, and
    still has current chunks. Moved, deleted, failed, or emptied — any of those and the duplicate
    grounds nothing while looking finished.
    """
    original = aliased(Source)
    original_has_chunks = (
        select(Chunk.id)
        .where(Chunk.source_id == original.id, Chunk.superseded_at.is_(None))
        .exists()
    )
    healthy = (
        select(original.id)
        .where(
            original.id == Source.duplicate_of_id,
            original.status == SourceStatus.DONE,
            original.learner_id == Source.learner_id,
            original.subject_id.is_not_distinct_from(Source.subject_id),
            original.topic_id.is_not_distinct_from(Source.topic_id),
            original_has_chunks,
        )
        .exists()
    )
    stmt = select(Source.id).where(_chunkless_done(), ~healthy).order_by(Source.updated_at)
    if learner_id is not None:
        stmt = stmt.where(Source.learner_id == learner_id)
    return list((await session.scalars(stmt)).all())


async def release_duplicates(
    session: AsyncSession, source_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Put chunkless DONE sources back in the queue's reach, to re-ingest from their own file.

    Flushes, does not commit, does not enqueue: the caller dispatches the returned ids after its
    own commit, or leaves them for the reconcile sweep, which requeues stale PENDING sources. A
    source that is not a chunkless DONE source is skipped, which is what makes a second release
    before the worker runs a no-op.
    """
    if not source_ids:
        return []
    ids = list(
        (
            await session.scalars(
                select(Source.id).where(Source.id.in_(source_ids), _chunkless_done())
            )
        ).all()
    )
    if ids:
        await session.execute(
            update(Source)
            .where(Source.id.in_(ids))
            .values(
                status=SourceStatus.PENDING,
                duplicate_of_id=None,
                attempts=0,
                error=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        await session.flush()
    return ids
```

  In `ReconcileReport` add `recovered: int = 0`. In `reconcile_stranded`, before `await session.commit()` that precedes the requeue loop, add:

```python
    # Duplicates whose original stopped standing in (S77): released here and requeued with the
    # rest, so every way an original can go away is caught, including ones nothing reacts to.
    recovered = await release_duplicates(session, await stranded_duplicates(session))
```

  and change the requeue loop to iterate `[*stranded, *recovered]`; return `ReconcileReport(requeued=requeued, abandoned=abandoned, recovered=len(recovered))`; include `recovered` in the info log condition and message.

  `app/workers/reconcile.py`: print `f"requeued={report.requeued} abandoned={report.abandoned} recovered={report.recovered}"`.

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_duplicate_recovery.py tests/test_ingestion_recovery.py tests/test_text_dedup.py -q` → PASS.
Run: `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/ingestion.py app/workers/reconcile.py tests/test_duplicate_recovery.py
git status
git commit -m "feat(ingestion): the reconcile sweep recovers stranded text duplicates [S77]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Release at the known causes — curriculum reassignment and reindex [S77]

**Files:**
- Modify: `app/services/knowledge.py` (`CurriculumResult`, reassign loop in `create_subject_with_graph`), `app/api/v1/knowledge.py` (`commit_subject`), `app/services/reindex.py` (`ReindexPlan.stranded`, `plan`, `apply`, `render`)
- Test: `tests/test_duplicate_recovery.py`, `tests/test_reindex.py`

**Interfaces:**
- Consumes: `release_duplicates`, `stranded_duplicates` (Task 2).
- Produces: `CurriculumResult.released_source_ids: list[uuid.UUID]`; `ReindexPlan.stranded: list[StaleSource]`; `ReindexResult.released: list[uuid.UUID]`; `async def duplicates_of(session, source_ids) -> list[uuid.UUID]` in `app/services/ingestion.py`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_duplicate_recovery.py`:

```python
async def test_moving_an_original_into_a_new_subject_releases_its_duplicate(
    db_session: AsyncSession,
) -> None:
    from app.services import knowledge

    _, original, dup = await _pair(db_session)
    owner = (await _get(db_session, original)).learner_id

    result = await knowledge.create_subject_with_graph(
        db_session,
        subject_name=f"Colour {uuid.uuid4().hex[:6]}",
        subject_description=None,
        topics_data=[],
        source_ids=[original],
        learner_id=owner,
        private_source_derived=True,
    )

    assert result.released_source_ids == [dup]
    assert (await _get(db_session, dup)).status == SourceStatus.PENDING


async def test_moving_a_duplicate_away_from_its_original_releases_it(
    db_session: AsyncSession,
) -> None:
    from app.services import knowledge

    _, original, dup = await _pair(db_session)
    owner = (await _get(db_session, original)).learner_id

    result = await knowledge.create_subject_with_graph(
        db_session,
        subject_name=f"Color {uuid.uuid4().hex[:6]}",
        subject_description=None,
        topics_data=[],
        source_ids=[dup],
        learner_id=owner,
        private_source_derived=True,
    )

    assert result.released_source_ids == [dup]
```

If `create_subject_with_graph` needs other required arguments or a different `topics_data` shape, read its signature and pass the minimal valid values — the assertion is the point.

Append to `tests/test_reindex.py`:

```python
async def test_reindex_lists_and_releases_stranded_duplicates(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    original = await _done_source(db_session, learner, b"The colour of the fibre was analysed.")
    dup = await _done_source(db_session, learner, b"The color of the fiber was analyzed.")
    original_id, dup_id = original.id, dup.id
    await db_session.delete(await db_session.get(Source, original_id))
    await db_session.commit()
    queue = _Queue()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session,
        fake_llm_client(),
        found,
        space=_space(),
        reextract=False,
        limit=0,
        enqueue=queue,
        settings=get_settings(),
    )

    assert [s.source_id for s in found.stranded] == [dup_id]
    assert result.released == [dup_id], "not counted against --limit"
    assert queue.enqueued == [dup_id]
    assert "stranded" in reindex.render(found, result)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_duplicate_recovery.py tests/test_reindex.py -q -k "moving or stranded"`
Expected: FAIL — `AttributeError: 'CurriculumResult' object has no attribute 'released_source_ids'` / `'ReindexPlan' object has no attribute 'stranded'`.

- [ ] **Step 3: Implement.**

`app/services/ingestion.py`:

```python
async def duplicates_of(session: AsyncSession, source_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Sources recorded as text duplicates of any of ``source_ids``."""
    if not source_ids:
        return []
    return list(
        (
            await session.scalars(select(Source.id).where(Source.duplicate_of_id.in_(source_ids)))
        ).all()
    )
```

`app/services/knowledge.py`: add `released_source_ids: list[uuid.UUID] = field(default_factory=list)` to `CurriculumResult` (import `field` if it is a dataclass; if it is a Pydantic model use `Field(default_factory=list)`). After the `if reassigned:` block and before the commit:

```python
    # A moved source no longer shares a scope with its text duplicates, and a moved duplicate no
    # longer shares one with its original (S77). Either way the duplicate grounds nothing, so it
    # is released to re-ingest from its own file.
    released = await ingestion.release_duplicates(
        session, [*reassigned, *await ingestion.duplicates_of(session, reassigned)]
    )
```

and return `CurriculumResult(subject=subject, reassigned_source_ids=reassigned, released_source_ids=released)`. If importing `app.services.ingestion` from `knowledge` is circular (ingestion imports knowledge), import it inside the function.

`app/api/v1/knowledge.py` `commit_subject`: add parameter `enqueue: IngestionEnqueuerDep` (import it from `app.api.deps`) and after the retag loop:

```python
    for source_id in result.released_source_ids:
        await ingestion_svc.dispatch(enqueue, source_id)
```

`app/services/reindex.py`:
- `ReindexPlan`: `stranded: list[StaleSource] = field(default_factory=list)`; `ReindexResult`: `released: list[uuid.UUID] = field(default_factory=list)`.
- `plan`: `stranded=[StaleSource(s.id, s.learner_id, s.origin, 0) for s in await _sources(session, await ingestion.stranded_duplicates(session, learner_id=learner_id))]` where `_sources` loads `Source` rows by id in the same order:

```python
async def _sources(session: AsyncSession, ids: list[uuid.UUID]) -> list[Source]:
    by_id = {s.id: s for s in (await session.scalars(select(Source).where(Source.id.in_(ids)))).all()}
    return [by_id[i] for i in ids if i in by_id]
```

- `apply`, right after the scope-repair commit:

```python
    # Scope repairs strand duplicates the same way a reassignment does (S77); and stranded ones
    # found by the plan are released here. Neither costs a model call, so --limit does not apply.
    repaired = [fix.source_id for fix in found.scope]
    released = await ingestion.release_duplicates(
        session,
        [
            *(s.source_id for s in found.stranded),
            *repaired,
            *await ingestion.duplicates_of(session, repaired),
        ],
    )
    await session.commit()
    for source_id in released:
        await ingestion.dispatch(enqueue, source_id)
    result.released = released
```

- `render`: after the scope lines add `f"stranded duplicates: {len(found.stranded)} sources (original gone or moved)"` plus one line per source, and in the result section `f"released {len(result.released)}"`.

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_duplicate_recovery.py tests/test_reindex.py tests/test_curriculum.py tests/test_source_scope.py -q` → PASS.
Run: `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/ingestion.py app/services/knowledge.py app/api/v1/knowledge.py app/services/reindex.py tests/test_duplicate_recovery.py tests/test_reindex.py
git status
git commit -m "feat(sources): moving a source releases the duplicates it strands [S77]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Structure-aware chunking [S27]

**Files:**
- Create: `app/rag/structure.py`
- Modify: `app/rag/chunking.py` (`chunk_units`, module docstring's "What this still does not do" paragraph), `app/rag/pipeline.py` (`PIPELINE_VERSION = 2`)
- Test: `tests/test_structure.py` (create), `tests/test_chunk_structure.py` (replace the two "limits" tests), `tests/test_chunk_versions.py` (if it pins the version value)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Segment:
      kind: Literal["prose", "code", "table", "math"]
      text: str
      start: int
      open: str | None = None    # code: opening fence line; math: opening delimiter line
      close: str | None = None   # code/math: closing line
      header: str | None = None  # table: header line(s)
  def segment(text: str) -> list[Segment]
  BLOCK_CAP_FACTOR = 4
  ```

- [ ] **Step 1: Write the failing tests** — `tests/test_structure.py`:

```python
"""Blocks survive chunking whole, or split where a reader can still use each part (S27)."""

from app.rag import pipeline
from app.rag.adapters.base import ExtractedUnit
from app.rag.chunking import DEFAULT_SIZE, chunk_units
from app.rag.structure import segment

CAP = 4 * DEFAULT_SIZE


def _chunks(text: str) -> list[ExtractedUnit]:
    return chunk_units([ExtractedUnit(text=text)])


def test_segments_cover_the_text_and_find_each_kind() -> None:
    text = (
        "Intro line.\n"
        "```python\nx = 1\n```\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
        "$$\nE = mc^2\n$$\n"
        "\\begin{align*}\na &= b\n\\end{align*}\n"
        "Outro."
    )
    kinds = [s.kind for s in segment(text)]
    assert kinds == ["prose", "code", "table", "math", "math", "prose"]
    assert "".join(s.text for s in segment(text)) == text


def test_an_unclosed_fence_runs_to_the_end() -> None:
    [prose, code] = segment("before\n```\nnever closed\nstill code")
    assert prose.kind == "prose" and code.kind == "code"
    assert code.text.endswith("still code")


def test_math_inside_a_fence_stays_code() -> None:
    assert [s.kind for s in segment("```\n$$\nnot math\n$$\n```")] == ["code"]


def test_a_block_longer_than_a_window_is_one_chunk() -> None:
    code = "```python\n" + "\n".join(f"step_{n}()" for n in range(150)) + "\n```"
    assert DEFAULT_SIZE < len(code) < CAP

    [chunk] = _chunks(code)

    assert chunk.text == code
    assert chunk.locator["structure"] == "code"
    assert "part" not in chunk.locator


def test_an_oversize_table_repeats_its_header_in_every_part() -> None:
    header = "| name | value |\n|---|---|"
    rows = "\n".join(f"| row {n} | {n * 7} |" for n in range(400))
    parts = _chunks(f"{header}\n{rows}")

    assert len(parts) > 1
    assert all(p.text.startswith(header) for p in parts)
    assert all(len(p.text) <= CAP for p in parts)
    assert [p.locator["part"] for p in parts] == [f"{i}/{len(parts)}" for i in range(1, len(parts) + 1)]
    assert all(p.locator["structure"] == "table" for p in parts)


def test_oversize_code_is_refenced_with_its_language() -> None:
    code = "```python\n" + "\n".join(f"call_number_{n}()" for n in range(400)) + "\n```"
    parts = _chunks(code)

    assert len(parts) > 1
    assert all(p.text.startswith("```python\n") and p.text.endswith("\n```") for p in parts)
    assert all(len(p.text) <= CAP for p in parts)


def test_oversize_math_is_rewrapped_in_its_environment() -> None:
    body = "\n".join(f"x_{n} &= y_{n} + z_{n} \\\\" for n in range(400))
    parts = _chunks(f"\\begin{{align}}\n{body}\n\\end{{align}}")

    assert len(parts) > 1
    assert all(
        p.text.startswith("\\begin{align}\n") and p.text.endswith("\n\\end{align}") for p in parts
    )


def test_prose_only_text_chunks_exactly_as_before() -> None:
    prose = "\n\n".join(f"Paragraph {n} says something about cells." * 5 for n in range(40))
    from app.rag.chunking import _windows, normalize

    expected = [text for _, text in _windows(normalize(prose), size=DEFAULT_SIZE, overlap=150)]

    assert [c.text for c in _chunks(prose)] == expected
    assert all("structure" not in c.locator for c in _chunks(prose))


def test_the_pipeline_version_moved_because_chunk_text_changes() -> None:
    assert pipeline.PIPELINE_VERSION == 2
```

In `tests/test_chunk_structure.py`, replace `test_a_block_longer_than_a_window_is_still_split_in_half` and `test_nothing_records_that_a_chunk_contains_a_table` (the "limits" section) with:

```python
def test_a_fenced_block_longer_than_a_window_is_no_longer_cut_in_half() -> None:
    """The limit this section used to exercise, now lifted (S27): the block is one chunk."""
    fenced = "```\n" + "\n".join(f"    step_{n}()" for n in range(150)) + "\n```"

    [piece] = chunk_units([ExtractedUnit(text=fenced)])

    assert piece.text.count("```") == 2


def test_a_table_chunk_says_it_is_a_table() -> None:
    units = chunk_units([ExtractedUnit(text=TABLE)])

    assert any(unit.locator.get("structure") == "table" for unit in units)
```

(Check `TABLE` in that file is a pipe table; if it is tab-separated, build a small pipe table inline instead.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_structure.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.structure'`.

- [ ] **Step 3: Implement `app/rag/structure.py`:**

```python
"""Where a document's structured blocks are (S27) — code, tables, display math.

Chunking kept the shape of a block but cut it wherever the window ran out, so the second half
of a table had no header and a derivation lost its first line. This finds the blocks so the
chunker can keep each one whole, or split it where every part still reads as what it is.

Line-based and deliberately literal: fenced code, pipe tables, ``$$``/``\\[``/``\\begin{…}``
math. The block that opens first wins and nothing nests, so ``$$`` inside a code fence is
code. Tab-separated tables are not detected — telling them from tab-indented prose needs a
heuristic nobody has measured.
"""

import re
from dataclasses import dataclass
from typing import Literal

_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s|:\-]+\|?\s*$")
_ENVS = "equation|align|gather|multline|eqnarray"
_BEGIN = re.compile(rf"^\s*\\begin\{{({_ENVS})\*?\}}")


@dataclass(frozen=True)
class Segment:
    kind: Literal["prose", "code", "table", "math"]
    text: str
    start: int
    open: str | None = None
    close: str | None = None
    header: str | None = None


def _math_close(line: str) -> str | None:
    """The line that closes a math block opened by ``line``, or None if it opens none."""
    stripped = line.strip()
    if stripped.startswith("$$"):
        return "$$"
    if stripped.startswith("\\["):
        return "\\]"
    begin = _BEGIN.match(line)
    if begin:
        star = "*" if line.strip().startswith(f"\\begin{{{begin.group(1)}*}}") else ""
        return f"\\end{{{begin.group(1)}{star}}}"
    return None


def segment(text: str) -> list[Segment]:
    """Split ``text`` into contiguous prose and block segments that concatenate back to it."""
    lines = text.splitlines(keepends=True)
    out: list[Segment] = []
    prose: list[str] = []
    offset = 0
    prose_start = 0
    i = 0

    def flush_prose(at: int) -> None:
        if prose:
            out.append(Segment("prose", "".join(prose), prose_start))
            prose.clear()

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        close = None if fence else _math_close(line)
        if fence or close is not None:
            flush_prose(offset)
            start = offset
            j = i + 1
            if fence:
                marker = fence.group(1)
                while j < len(lines) and not lines[j].lstrip().startswith(marker):
                    j += 1
            else:
                # A one-line "$$ x $$" closes on its own line.
                one_line = line.strip()
                if close == "$$" and one_line.count("$$") >= 2:
                    j = i
                else:
                    while j < len(lines) and not lines[j].strip().startswith(close):
                        j += 1
            end = min(j, len(lines) - 1)
            block = "".join(lines[i : end + 1])
            opener = line.rstrip("\n")
            closer = lines[end].rstrip("\n") if end > i else None
            out.append(
                Segment("code" if fence else "math", block, start, open=opener, close=closer)
            )
            offset += len(block)
            i = end + 1
            prose_start = offset
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) and _TABLE_ROW.match(lines[i + 1]):
            flush_prose(offset)
            start = offset
            j = i
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                j += 1
            block = "".join(lines[i:j])
            header = lines[i].rstrip("\n")
            if _TABLE_SEPARATOR.match(lines[i + 1]):
                header += "\n" + lines[i + 1].rstrip("\n")
            out.append(Segment("table", block, start, header=header))
            offset += len(block)
            i = j
            prose_start = offset
            continue
        if not prose:
            prose_start = offset
        prose.append(line)
        offset += len(line)
        i += 1
    flush_prose(offset)
    return out
```

(For an unclosed fence or math block, `closer` is the last line of the text and `close` is set to it; the split logic below only re-wraps when both `open` and `close` are real delimiters — see `_wrap`.)

In `app/rag/chunking.py`, add `from app.rag.structure import Segment, segment` and `BLOCK_CAP_FACTOR = 4`, and replace `chunk_units` with:

```python
def chunk_units(
    units: list[ExtractedUnit], *, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[ExtractedUnit]:
    """Subdivide each unit into chunks, preserving + extending its locator.

    Prose is windowed as before. A structured block (S27) is one chunk up to
    ``BLOCK_CAP_FACTOR × size``, and beyond that is split at line breaks into parts that each
    carry the block's context — a table's header, a code block's fence, a math environment's
    delimiters — so every part still reads as what it is.
    """
    cap = BLOCK_CAP_FACTOR * size
    out: list[ExtractedUnit] = []
    for unit in units:
        for seg in segment(normalize(unit.text)):
            if seg.kind == "prose":
                pieces = [
                    (seg.start + start, piece, {})
                    for start, piece in _windows(seg.text, size=size, overlap=overlap)
                ]
            else:
                pieces = _block_pieces(seg, cap)
            for start, piece, extra in pieces:
                out.append(
                    ExtractedUnit(
                        text=piece,
                        locator={**unit.locator, "char_start": start, **extra},
                        method=unit.method,  # keep the unit's extraction method (e.g. OCR)
                    )
                )
    return out


def _block_pieces(seg: Segment, cap: int) -> list[tuple[int, str, dict]]:
    """A block as one piece, or as self-contained parts when it is longer than ``cap``."""
    text = seg.text.strip("\n")
    if not text.strip():
        return []
    if len(text) <= cap:
        return [(seg.start, text, {"structure": seg.kind})]
    head, tail, body = _frame(seg, text)
    room = cap - len(head) - len(tail)
    parts: list[tuple[int, list[str]]] = []
    offset = seg.start + (len(head) if head else 0)
    current: list[str] = []
    current_start = offset
    used = 0
    for line in body:
        piece_line = line[:room] if len(line) > room else line  # an overlong line is cut hard
        cost = len(piece_line) + 1
        if current and used + cost > room:
            parts.append((current_start, current))
            current, used, current_start = [], 0, offset
        current.append(piece_line)
        used += cost
        offset += len(line) + 1
    if current:
        parts.append((current_start, current))
    n = len(parts)
    return [
        (start, f"{head}{chr(10).join(lines)}{tail}", {"structure": seg.kind, "part": f"{i}/{n}"})
        for i, (start, lines) in enumerate(parts, start=1)
    ]


def _frame(seg: Segment, text: str) -> tuple[str, str, list[str]]:
    """The context every part repeats, and the lines between it."""
    lines = text.split("\n")
    if seg.kind == "table" and seg.header:
        header_lines = seg.header.count("\n") + 1
        return seg.header + "\n", "", lines[header_lines:]
    if seg.kind in ("code", "math") and seg.open and seg.close and len(lines) >= 2:
        return seg.open + "\n", "\n" + seg.close, lines[1:-1]
    return "", "", lines
```

For an *unclosed* fence, `seg.close` is the block's last line rather than a delimiter; guard `_frame` so it only re-wraps when `seg.close.strip()` is a closing delimiter (starts with the same fence marker for code, or equals the expected math closer). Store the expected closer on the segment instead of the last line if that is simpler: set `close` to the matching fence marker / `_math_close(line)` result only when the block actually closed, else `None`. Adjust `segment` accordingly and keep `test_an_unclosed_fence_runs_to_the_end` green.

Update the module docstring's last paragraph to say blocks are now detected (`app.rag.structure`) and kept whole or split with context, and that tab-separated tables are still not detected.

`app/rag/pipeline.py`: `PIPELINE_VERSION = 2` with the comment "2: structure-aware chunking (S27)".

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_structure.py tests/test_chunk_structure.py tests/test_chunks.py tests/test_chunk_versions.py tests/test_reindex.py -q` → PASS. The property test `test_chunking_always_terminates_and_never_drops_text` must stay green unchanged.
Run: `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/rag/structure.py app/rag/chunking.py app/rag/pipeline.py tests/test_structure.py tests/test_chunk_structure.py
git status
git commit -m "feat(rag): keep code, tables and display math whole, or split them with context [S27]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Reading notes for the tutor, lessons and the citation API [S27]

**Files:**
- Modify: `app/rag/extraction_quality.py` (`reading_note`), `app/services/grounding.py` (`format_grounding`, `instruction` callers), `app/services/content.py` (`_build_prompt`, grounded system prompt), `app/schemas/source.py` (`ChunkRead.reading_note`)
- Test: `tests/test_reading_notes.py` (create)

**Interfaces:**
- Produces: `def reading_note(provenance: Mapping) -> str | None`; `READING_NOTE_RULE: str` in `app/services/grounding.py`; `ChunkRead.reading_note: str | None`.

- [ ] **Step 1: Write the failing tests** — `tests/test_reading_notes.py`:

```python
"""Machine-read passages say so — to the tutor and to the learner (S27)."""

import uuid

from app.rag.extraction_quality import reading_note
from app.rag.retrieval import RetrievalHit
from app.schemas.source import ChunkRead
from app.services.grounding import READING_NOTE_RULE, format_grounding

CLEAN = {"replacement_chars": 0, "control_chars": 0}


def _hit(text: str, provenance: dict) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), text=text, provenance=provenance, score=1.0
    )


def test_ocr_and_asr_are_named() -> None:
    assert reading_note({"method": "ocr"}) == "read from a scan or image; wording may contain errors"
    assert reading_note({"method": "asr"}) == "transcribed from audio; wording may contain errors"


def test_undecodable_characters_are_named() -> None:
    note = reading_note({"method": "text", "extraction": {**CLEAN, "replacement_chars": 3}})
    assert note == "some characters could not be read"
    assert reading_note({"extraction": {**CLEAN, "control_chars": 1}}) == note


def test_several_facts_are_joined() -> None:
    note = reading_note({"method": "ocr", "extraction": {**CLEAN, "replacement_chars": 1}})
    assert note == (
        "read from a scan or image; wording may contain errors; some characters could not be read"
    )


def test_clean_born_digital_text_has_no_note() -> None:
    assert reading_note({"method": "text", "extraction": CLEAN}) is None
    assert reading_note({}) is None


def test_a_noted_passage_is_marked_and_the_rule_is_added() -> None:
    section = format_grounding(
        [_hit("clean words", {"method": "text"}), _hit("scanned words", {"method": "ocr"})],
        sources_only=False,
    )
    assert "[1] clean words" in section
    assert "[2] (read from a scan or image; wording may contain errors) scanned words" in section
    assert READING_NOTE_RULE in section


def test_no_rule_when_nothing_is_noted() -> None:
    section = format_grounding([_hit("clean", {"method": "text"})], sources_only=False)
    assert READING_NOTE_RULE not in section


def test_the_citation_api_carries_the_note() -> None:
    read = ChunkRead(id=uuid.uuid4(), ordinal=0, text="x", provenance={"method": "asr"})
    assert read.model_dump()["reading_note"] == (
        "transcribed from audio; wording may contain errors"
    )
```

Add to `tests/test_content.py`, beside the other prompt tests (it has `_spy_on_prompts`, `_kc`, `_learner`, `_source`, `_chunk`):

```python
async def test_lesson_snippets_carry_reading_notes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    chunk = await _chunk(db_session, await _source(db_session, learner), "mitochondria make ATP", 0)
    chunk.provenance = {**chunk.provenance, "method": "ocr"}
    await db_session.flush()
    client = _client()
    seen = _spy_on_prompts(client, monkeypatch)

    await svc.generate_block(
        db_session, client, learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    system, user = seen[0]
    assert "(read from a scan or image; wording may contain errors) mitochondria" in user
    assert system is not None and READING_NOTE_RULE in system
```

(import `READING_NOTE_RULE` from `app.services.grounding`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_reading_notes.py -q`
Expected: FAIL — `ImportError: cannot import name 'reading_note'`.

- [ ] **Step 3: Implement.** `app/rag/extraction_quality.py` (add `from collections.abc import Mapping`):

```python
_METHOD_NOTES = {
    "ocr": "read from a scan or image; wording may contain errors",
    "asr": "transcribed from audio; wording may contain errors",
}


def reading_note(provenance: Mapping) -> str | None:
    """What a reader should know about how this passage was read — facts only (S27).

    How the text was obtained, and whether the decoder had to give up on characters. The ratio
    indicators are left out on purpose: none has a measured threshold, and as written they fire
    on ordinary English (single-letter words count as isolated letters).
    """
    notes = []
    method_note = _METHOD_NOTES.get(str(provenance.get("method", "")))
    if method_note:
        notes.append(method_note)
    extraction = provenance.get("extraction") or {}
    if extraction.get("replacement_chars") or extraction.get("control_chars"):
        notes.append("some characters could not be read")
    return "; ".join(notes) or None
```

`app/services/grounding.py` (import `reading_note`):

```python
READING_NOTE_RULE = (
    "Passages marked with a reading note were machine-read and may contain errors: do not "
    "present exact figures, names or formulas from them as certain, and say so where it matters."
)


def _passage(i: int, hit: RetrievalHit) -> str:
    note = reading_note(hit.provenance)
    return f"[{i}] ({note}) {hit.text}" if note else f"[{i}] {hit.text}"
```

and in `format_grounding` build `passages` with `_passage(i, hit)`, and append the rule when any hit has a note:

```python
    rule = instruction(sources_only=sources_only, has_passages=bool(hits))
    if not hits:
        return rule
    if any(reading_note(hit.provenance) for hit in hits):
        rule = f"{rule} {READING_NOTE_RULE}"
    passages = "\n".join(_passage(i, hit) for i, hit in enumerate(hits, start=1))
```

`app/services/content.py`: in `_build_prompt`, render each snippet as `f"[{i}] ({note}) {hit.text}"` when `reading_note(hit.provenance)` is set (keep 0-based indices as today); in `generate_block`, when grounding is non-empty and any hit has a note, append `" " + READING_NOTE_RULE` to `system` (before computing the cache key, so the key covers it).

`app/schemas/source.py` `ChunkRead` (import `reading_note`):

```python
    @computed_field
    @property
    def reading_note(self) -> str | None:
        """How this passage was read, when that should temper trust in it (S27)."""
        return reading_note(self.provenance)
```

If the property name clashes with the imported function inside the class body, import the module: `from app.rag import extraction_quality` and call `extraction_quality.reading_note(...)`.

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_reading_notes.py tests/test_grounding.py tests/test_content.py tests/test_chat.py -q` → PASS.
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/rag/extraction_quality.py app/services/grounding.py app/services/content.py app/schemas/source.py frontend/src/api/schema.d.ts tests/test_reading_notes.py tests/test_content.py
git status
git commit -m "feat(tutor): machine-read passages carry a reading note [S27]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Frontend — duplicate label and the citation note [S77, S27]

**Files:**
- Modify: `frontend/src/components/uploads/SourceList.tsx`, `frontend/src/components/chat/CitationPane.tsx`
- Test: `frontend/src/components/uploads/SourceList.test.tsx` (create), `frontend/src/components/chat/CitationPane.test.tsx`

**Interfaces:**
- Consumes: `SourceRead.duplicate_of_id` (Task 1), `ChunkRead.reading_note` (Task 5).
- Produces: `duplicateLabel(source, sources): string | null` exported from `SourceList.tsx`; `CitationBody` prop `note?: string | null`.

- [ ] **Step 1: Write the failing tests.** `SourceList.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { duplicateLabel } from "./SourceList";

const base = { kind: "file", content_type: "text/plain", error: null, subject_id: null,
  topic_id: null, created_at: "2026-09-26T00:00:00Z" };

describe("duplicateLabel", () => {
  it("names the original when it is in the list", () => {
    const original = { ...base, id: "a", origin: "uk.txt", status: "done", duplicate_of_id: null };
    const dup = { ...base, id: "b", origin: "us.txt", status: "done", duplicate_of_id: "a" };
    expect(duplicateLabel(dup, [original, dup])).toBe("Same text as uk.txt");
  });

  it("still says so when the original is not in the list", () => {
    const dup = { ...base, id: "b", origin: "us.txt", status: "done", duplicate_of_id: "gone" };
    expect(duplicateLabel(dup, [dup])).toBe("Same text as another of your sources");
  });

  it("says nothing for an ordinary source", () => {
    const src = { ...base, id: "a", origin: "uk.txt", status: "done", duplicate_of_id: null };
    expect(duplicateLabel(src, [src])).toBeNull();
  });
});
```

(If `Source` needs more fields to type-check, cast the fixtures `as Source` via `import type { components } from "../../api/schema"`.)

Append to `CitationPane.test.tsx`:

```tsx
describe("CitationBody reading note", () => {
  it("shows how a passage was read", () => {
    render(
      <CitationBody
        origin="scan.pdf"
        text="blurry words"
        note="read from a scan or image; wording may contain errors"
      />,
    );
    expect(
      screen.getByText("Read from a scan or image; wording may contain errors"),
    ).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/uploads/SourceList.test.tsx src/components/chat/CitationPane.test.tsx`
Expected: FAIL — `duplicateLabel` is not exported; the note is not rendered.

- [ ] **Step 3: Implement.** `SourceList.tsx`:

```tsx
/** A text duplicate is stored without passages of its own; say whose text it shares (S77)
 * rather than letting it look like a finished source with nothing in it. */
export function duplicateLabel(source: Source, sources: Source[]): string | null {
  if (!source.duplicate_of_id) return null;
  const original = sources.find((s) => s.id === source.duplicate_of_id);
  return original ? `Same text as ${original.origin}` : "Same text as another of your sources";
}
```

`SourceRow` takes `sources: Source[]` as a prop (pass it from `SourceList`) and renders, under the origin span, `{label && <span className="text-caption text-base-content/50">{label}</span>}` where `const label = duplicateLabel(source, sources);` (wrap origin and label in a `flex flex-col min-w-0` div).

`CitationPane.tsx`: `CitationBody` gains `note?: string | null` and renders, after the superseded line,

```tsx
{note && (
  <p className="text-caption text-base-content/60">
    {note.charAt(0).toUpperCase() + note.slice(1)}
  </p>
)}
```

and `CitationPane` passes `note={chunk?.reading_note ?? null}`.

- [ ] **Step 4: Run tests and the build**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build`, plus `npx prettier --check` and `npx eslint` on the changed files → green.

- [ ] **Step 5: Commit (two commits, one id each)**

```bash
git add frontend/src/components/uploads/SourceList.tsx frontend/src/components/uploads/SourceList.test.tsx
git status
git commit -m "feat(web): a duplicate source says whose text it shares [S77]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git add frontend/src/components/chat/CitationPane.tsx frontend/src/components/chat/CitationPane.test.tsx
git status
git commit -m "feat(web): the citation pane says how a passage was read [S27]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Tracker [S27]

**Files:** Modify `docs/guru-suggestions-tracker.md` (rows S77, S27, S18), `docs/RUNBOOK.md` §15.

- [ ] **Step 1:** S77 → **Partial**: duplicate relation is a foreign key; stranded duplicates (original deleted, moved, failed or emptied) are released at curriculum reassignment and reindex scope repair and caught by the reconcile sweep and `poe reindex`; they re-ingest from their own file; the uploads list names the shared original. Remaining: evaluate selected scanned/digital pairs (testing phase). Evidence: `app/services/ingestion.py`, `tests/test_duplicate_recovery.py`.
- [ ] **Step 2:** S27 → **Partial**: structure-aware chunking (code, pipe tables, display math whole up to 4× the window, split with context beyond; `PIPELINE_VERSION` 2); reading notes from provenance facts (OCR, ASR, undecodable characters) reach the tutor, lesson prompts and the citation pane. Remaining: fixture accuracy evaluation; calibrating the ratio indicators (S18). Evidence: `app/rag/structure.py`, `app/rag/extraction_quality.py`, `tests/test_structure.py`, `tests/test_reading_notes.py`.
- [ ] **Step 3:** S18: append "Extraction ratio indicators (`isolated_letter_ratio`, `vowelless_word_ratio`, `runaway_token_ratio`) are stored and unread; they need thresholds (single-letter English words already trip the first)."
- [ ] **Step 4:** RUNBOOK §15: add "`PIPELINE_VERSION` 2 (structure-aware chunking): run `poe reindex --apply --reextract` when ready; the dry run lists every source. Stranded duplicates are listed and released by `--apply`, uncapped by `--limit`."
- [ ] **Step 5:** `uv run poe check` → green, then:

```bash
git add docs/guru-suggestions-tracker.md docs/RUNBOOK.md
git status
git commit -m "docs: record duplicate recovery and structured extraction in the tracker [S27]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
