# Resumable, Strictly Bounded Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingestion that never pays twice for work that already succeeded, retries concept tagging instead of silently dropping it, and holds an exact global and per-learner job ceiling.

**Architecture:** `pipeline.run` becomes three committed stages (extract → saved artifact; embed → `staged_chunks` in committed windows; publish → one transaction making the chunks live and the source `done`), resumed from `sources.stage`. Tagging becomes a fourth stage after publish that a refusal leaves pending for the reconcile sweep. `ingest_source` takes two Postgres advisory-lock slots (global and per-learner) on a dedicated connection before claiming, and dispatches the next pending source when it finishes.

**Tech Stack:** Python 3.13, SQLAlchemy async + asyncpg, Postgres advisory locks, pgvector, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-resumable-ingestion-design.md`

## Global Constraints

- Branch `feat/workstream-2` (open PR #44). Never reset, amend, rebase, squash or force-push.
- Stage only the task's files by name, then `git status`.
- Commit subjects end `[S37]`; every commit message ends with the exact line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Never read or print `.env` or any secret; never set `GURU_JEV_SMOKE`; no paid model runs.
- Artifact key, verbatim: `ingest/{source_id}/extract.json.gz` (gzip JSON list of `ExtractedUnit.model_dump()`).
- `sources.stage` values: NULL (nothing to resume), `"embed"`, `"tag"`.
- New setting `ingest_max_jobs_per_learner: int = Field(default=2, gt=0, le=16)`; `ingest_max_concurrent_jobs` (4) becomes exact.
- Advisory-lock namespaces: global `0x494E4753` ("INGS"), learner `0x494E474C` ("INGL"); learner key `((learner_id.int & 0x0FFFFFFF) << 4) | j`.
- Gates: `uv run poe check`, `uv run poe format-check`; after the migration `uv run poe db-upgrade && uv run poe db-check` and `uv run python -m tests.testdb`.
- Beartype runs at test time: floats as floats.
- Every finished piece updates `docs/guru-suggestions-tracker.md` (Task 6).

## Review Focus

1. A job that dies *after* publish commits but before tagging must resume at tagging, not re-extract or re-embed. Pinned in Task 3 (`test_a_refused_tag_leaves_the_source_done_and_searchable` + `test_the_tag_retry_does_not_embed_again`).
2. A source being re-processed must keep answering with its old chunks until the new ones publish. Pinned in Task 2 (`test_a_reprocessed_source_keeps_its_old_chunks_until_publish`).
3. A failed or crashed job must never leave its advisory slots held on a pooled connection (the pool reuses connections; a session lock survives `close()`). Pinned in Task 4 (`test_released_slots_can_be_taken_again`, `test_ingest_releases_its_slots_even_when_the_job_fails`).
4. A concept-tagging refusal wrapped by DSPy must still be recognised as a refusal. Pinned in Task 3 (`test_a_tagging_refusal_is_not_swallowed`).
5. Deleting a source or an account mid-ingest must not leave the saved extraction behind. Pinned in Task 5.

---

### Task 1: Data — `sources.stage`, `staged_chunks`, the per-learner setting

**Files:**
- Create: `db/migrations/versions/0076_resumable_ingestion.py`
- Modify: `app/models/source.py` (`SourceStage`, `Source.stage`, `StagedChunk`)
- Modify: `app/core/config.py`, `tests/eval/reliability/knobs.py`, `app/services/retention.py` (`RETENTION` entry)
- Test: `tests/test_migrations_with_data.py` (add), `tests/test_ingestion_stages.py` (create)

**Interfaces:**
- Produces: `SourceStage(StrEnum)` with `EMBED = "embed"`, `TAG = "tag"`; `Source.stage: Mapped[str | None]`; `StagedChunk` model (table `staged_chunks`: `id`, `source_id` FK CASCADE, `ordinal`, `text`, `embedding` Vector(`_EMBED_DIM`), `embedding_space`, `pipeline_version` int, `provenance` JSONB, timestamps; unique `(source_id, ordinal)`); `Settings.ingest_max_jobs_per_learner`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrations_with_data.py`:

```python
async def test_existing_sources_have_nothing_to_resume() -> None:
    """0076 (S37): a nullable stage and an empty staging table."""
    async with database_at("0075_profile_messages_since") as connect:
        await upgrade(SCRATCH, "0076_resumable_ingestion")
        conn = await connect()
        try:
            nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'sources' AND column_name = 'stage'"
            )
            staged = await conn.fetchval("SELECT count(*) FROM staged_chunks")
            assert (nullable, staged) == ("YES", 0)
        finally:
            await conn.close()
```

Create `tests/test_ingestion_stages.py`:

```python
"""Ingestion in committed stages, resumed where it stopped (S37)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.learner import Learner
from app.models.source import SourceKind, StagedChunk
from app.services import ingestion
from app.storage import InMemoryBlobStore

# Paragraphs long enough that the chunker (1000-char windows) makes several chunks.
LONG = "\n\n".join(
    f"Paragraph {i}: " + "The mitochondrion releases energy from glucose inside the cell. " * 18
    for i in range(5)
).encode()


async def _source(session: AsyncSession, store: InMemoryBlobStore, *, data: bytes = LONG, **kw):
    learner = Learner(handle=f"s-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=data,
        **kw,
    )


async def _staged(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(StagedChunk).where(StagedChunk.source_id == source_id)
        )
        or 0
    )


async def test_staged_chunks_go_with_their_source(db_session: AsyncSession) -> None:
    from app.models.source import Source

    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    db_session.add(
        StagedChunk(
            source_id=source.id,
            ordinal=0,
            text="x",
            embedding=[0.0] * get_settings().embed_dim,
            embedding_space="fake",
            pipeline_version=1,
            provenance={},
        )
    )
    await db_session.commit()

    await db_session.delete(await db_session.get(Source, source.id))
    await db_session.commit()

    assert await _staged(db_session, source.id) == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ingestion_stages.py tests/test_migrations_with_data.py -k "staged or resume" -q -p no:cacheprovider`
Expected: collection error — `ImportError: cannot import name 'StagedChunk'`.

- [ ] **Step 3: Implement**

In `app/models/source.py`, add beside `SourceStatus`:

```python
class SourceStage(StrEnum):
    """Where an interrupted ingestion resumes (S37); NULL means nothing to resume."""

    EMBED = "embed"  # extracted and saved; chunks being embedded into staged_chunks
    TAG = "tag"  # published; concept tags still to write
```

on `Source`, after `status`:

```python
    # Where the job resumes (S37). Set by each committed stage; NULL once tagging is done.
    stage: Mapped[str | None] = mapped_column(default=None)
```

and after `Chunk` (before `ChunkKC`):

```python
class StagedChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A chunk embedded but not yet published (S37).

    Nothing but the embed and publish stages reads this table, so retrieval — and every query
    filtering ``chunks.superseded_at IS NULL`` — never sees half a source. Publish copies the
    rows into ``chunks`` in one transaction and deletes them.
    """

    __tablename__ = "staged_chunks"
    __table_args__ = (UniqueConstraint("source_id", "ordinal", name="uq_staged_chunks_ordinal"),)

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(_EMBED_DIM))
    embedding_space: Mapped[str]
    pipeline_version: Mapped[int]
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
```

(import `UniqueConstraint` from `sqlalchemy` if the module lacks it; `StrEnum` is already used for `SourceStatus`).

Create `db/migrations/versions/0076_resumable_ingestion.py`:

```python
"""Resumable ingestion (S37): ``sources.stage`` and ``staged_chunks``.

``stage`` is where an interrupted job resumes — NULL (nothing to resume; every existing row),
``embed`` or ``tag``. ``staged_chunks`` holds embedded chunks until publish copies them into
``chunks``; nothing else reads it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings

revision: str = "0076_resumable_ingestion"
down_revision: str | Sequence[str] | None = "0075_profile_messages_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("stage", sa.String(), nullable=True))
    op.create_table(
        "staged_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(get_settings().embed_dim), nullable=False),
        sa.Column("embedding_space", sa.String(), nullable=False),
        sa.Column("pipeline_version", sa.Integer(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_id", "ordinal", name="uq_staged_chunks_ordinal"),
    )
    op.create_index("ix_staged_chunks_source_id", "staged_chunks", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_staged_chunks_source_id", table_name="staged_chunks")
    op.drop_table("staged_chunks")
    op.drop_column("sources", "stage")
```

Check the column types against how `chunks` was created (`grep -n "chunks" db/migrations/versions/*.py | head`) and match them — `Chunk.embedding_space` / `pipeline_version` types and the mixin's timestamp type — so `uv run poe db-check` reports nothing.

`app/core/config.py`, after `ingest_max_concurrent_jobs`:

```python
    # How many of the concurrent ingestion jobs one learner may hold at once (S37), so one
    # learner's pile of uploads cannot take every slot. Uncalibrated (S18).
    ingest_max_jobs_per_learner: int = Field(default=2, gt=0, le=16)
```

and amend the comment above `ingest_max_concurrent_jobs` from "a *soft* cap — see claim_source" to "an exact cap, taken as advisory-lock slots — see app/services/ingest_slots.py".

`tests/eval/reliability/knobs.py`: a `Knob(id="ingest.max_jobs_per_learner", where="app.core.config.Settings.ingest_max_jobs_per_learner", value=2.0, governs="how many ingestion jobs one learner may run at once (S37)", settled_by="alpha upload patterns: how many files learners add at once, and the wait that causes")` and `"ingest.max_jobs_per_learner": float(s.ingest_max_jobs_per_learner)` in `live()`.

`app/services/retention.py` `RETENTION`, after the `chunks` entry:

```python
    StoreRetention(
        "staged_chunks",
        "deleted",
        "Cascades from the source; empty once a source is published (S37).",
    ),
```

- [ ] **Step 4: Migrate and run the tests**

Run: `uv run poe db-upgrade && uv run poe db-check && uv run python -m tests.testdb && uv run pytest tests/test_ingestion_stages.py tests/test_migrations_with_data.py tests/test_retention.py tests/eval/test_reliability_knobs.py -q -p no:cacheprovider`
Expected: db-check "No new upgrade operations detected."; all tests pass.

- [ ] **Step 5: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add db/migrations/versions/0076_resumable_ingestion.py app/models/source.py app/core/config.py tests/eval/reliability/knobs.py app/services/retention.py tests/test_migrations_with_data.py tests/test_ingestion_stages.py
git commit -m "feat(ingestion): a source records where its ingestion resumes [S37]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: Extract, embed and publish as committed stages

**Files:**
- Modify: `app/rag/pipeline.py` (`run` split into stages; artifact helpers)
- Modify: `app/services/ingestion.py` (`ingest_source` leaves completion to the pipeline; artifact drop; `reset_for_reingest`)
- Test: `tests/test_ingestion_stages.py` (add)

**Interfaces:**
- Consumes: `SourceStage`, `StagedChunk`, `Source.stage` (Task 1).
- Produces (in `app.rag.pipeline`): `artifact_key(source_id: uuid.UUID) -> str`; `run(...)` keeps its signature and return (chunk count) but now **commits** each stage and leaves the source `done` (stage `"tag"`, or NULL for a same-text duplicate) with `attempts = 0` and no lease; `tag_chunks(session, llm, source, rows, settings)` (renamed from `_tag_chunks`, now public). In `app.services.ingestion`: `drop_artifact(session, blobstore, source_id) -> None` (best-effort; a refusal becomes a `pending_erasures` BLOB row).

Behaviour notes for the implementer:
- `ingest_source` still wraps `pipeline.run` in the job timeout and `_record_failure` on any exception (tests patch `app.rag.pipeline.run`; keep calling it by that name). It no longer marks the source `done` itself.
- Tagging moves out of `run` in Task 3. In this task, `run` does **not** tag; `stage` is left `"tag"` after publish and Task 3 adds the stage that consumes it. (Between Task 2 and Task 3 a fresh ingest leaves chunks untagged — Task 3 restores tagging; `uv run poe check` is expected green only at the end of Task 3 if existing tagging tests assert tags after `ingest_source`. Ledger this if so.)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingestion_stages.py` (add imports: `from app.core.config import Settings`, `from app.llm import ModelRole`, `from app.llm.meter import ProviderUnavailable`, `from app.llm.providers.fake import FakeProvider`, `from app.llm.registry import LLMClient, ModelSpec, fake_llm_client`, `from app.models.source import Chunk, SourceStatus`, `from app.rag import pipeline`):

```python
SETTINGS = Settings(embed_batch_size=1, embed_concurrency=1)


class _FlakyEmbed(FakeProvider):
    """Refuses the Nth embed call once, and remembers every text it embedded."""

    def __init__(self, fail_on: int | None) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0
        self.embedded: list[str] = []

    async def embed(self, *, model, texts):
        self.calls += 1
        if self.calls == self.fail_on:
            raise ProviderUnavailable("down")
        self.embedded.extend(texts)
        return await super().embed(model=model, texts=texts)


class _CountingStore(InMemoryBlobStore):
    def __init__(self) -> None:
        super().__init__()
        self.downloads = 0

    async def download(self, key, dest):
        self.downloads += 1
        return await super().download(key, dest)


def _llm(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def _live_chunks(session: AsyncSession, source_id: uuid.UUID) -> list[Chunk]:
    rows = await session.scalars(
        select(Chunk)
        .where(Chunk.source_id == source_id, Chunk.superseded_at.is_(None))
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def test_a_retry_resumes_without_extracting_or_embedding_again(
    db_session: AsyncSession,
) -> None:
    store = _CountingStore()
    source = await _source(db_session, store)
    provider = _FlakyEmbed(fail_on=2)

    first = await ingestion.ingest_source(
        db_session, store, _llm(provider), source.id, settings=SETTINGS
    )
    assert first is not None
    assert (first.status, first.stage) == (SourceStatus.PENDING, "embed")
    assert await _staged(db_session, source.id) == 1  # the batch before the refusal is kept
    assert await _live_chunks(db_session, source.id) == []  # nothing half-published

    embedded_before = len(provider.embedded)
    second = await ingestion.ingest_source(
        db_session, store, _llm(provider), source.id, settings=SETTINGS
    )

    assert second is not None and second.status == SourceStatus.DONE
    assert store.downloads == 1  # extracted once
    chunks = await _live_chunks(db_session, source.id)
    assert len(provider.embedded) - embedded_before == len(chunks) - 1  # only what was missing
    assert await _staged(db_session, source.id) == 0
    assert not await store.exists(pipeline.artifact_key(source.id))


async def test_a_reprocessed_source_keeps_its_old_chunks_until_publish(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    old = {c.id for c in await _live_chunks(db_session, source.id)}
    assert old

    await ingestion.reset_for_reingest(db_session, source.id)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=1)), source.id, settings=SETTINGS
    )

    assert {c.id for c in await _live_chunks(db_session, source.id)} == old


async def test_reprocessing_starts_over_and_clears_what_was_staged(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=2)), source.id, settings=SETTINGS
    )
    assert await _staged(db_session, source.id) == 1

    reset = await ingestion.reset_for_reingest(db_session, source.id)

    assert reset is not None and reset.stage is None
    assert await _staged(db_session, source.id) == 0


async def test_a_finished_source_is_done_with_its_chunks_and_nothing_staged(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store)

    done = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    assert done is not None
    assert (done.status, done.attempts, done.lease_expires_at) == (SourceStatus.DONE, 0, None)
    assert done.meta["chunk_count"] == len(await _live_chunks(db_session, source.id)) > 1
    assert await _staged(db_session, source.id) == 0
    assert not await store.exists(pipeline.artifact_key(source.id))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ingestion_stages.py -q -p no:cacheprovider`
Expected: the four new tests fail (`first.stage` is None; downloads == 2; `artifact_key` missing; attempts == 1 on the finished source).

- [ ] **Step 3: Split `pipeline.run` into stages**

In `app/rag/pipeline.py` (add imports `gzip`, `json`, `insert` from sqlalchemy, `ExtractedUnit` from the adapters base, `SourceStage`, `SourceStatus`, `StagedChunk`):

```python
def artifact_key(source_id: uuid.UUID) -> str:
    """Where a source's extracted text waits between the extract and embed stages (S37)."""
    return f"ingest/{source_id}/extract.json.gz"


def _pack(units: Sequence[ExtractedUnit]) -> bytes:
    return gzip.compress(json.dumps([u.model_dump() for u in units]).encode())


def _unpack(data: bytes) -> list[ExtractedUnit]:
    return [ExtractedUnit.model_validate(u) for u in json.loads(gzip.decompress(data))]
```

Replace `run` with an orchestrator plus three stage functions. `run` keeps its signature and docstring's budget paragraph, and becomes:

```python
async def run(
    session: AsyncSession,
    blobstore: BlobStore,
    llm: LLMClient,
    source: Source,
    *,
    transcriber: Transcriber | None = None,
    demuxer: MediaDemuxer | None = None,
    settings: Settings | None = None,
) -> int:
    """Ingest one source into chunks, resuming at ``source.stage`` (S37). Returns the chunk count.

    Each stage commits before the next: extract (the text is saved to the blob store), embed
    (chunks staged in committed windows), publish (one transaction makes them live and marks
    the source done). A retry, or a lapsed lease reclaimed by another worker, starts where the
    last commit left it, so OCR, transcription and embeddings are never paid for twice.

    Two budgets bound the work, both checked *before* the expensive step they guard:
    extracted characters before the text is saved, and chunk count before embedding.
    """
    settings = settings or get_settings()
    if source.stage is None:
        if not await _extract(session, blobstore, llm, source, transcriber, demuxer, settings):
            return 0  # a same-text duplicate: finished at extraction
    await _embed(session, blobstore, llm, source, settings)
    return await _publish(session, source)
```

`_extract` is the current body of `run` from the `blob_key` / adapter checks through the duplicate check, with these differences:

```python
async def _extract(session, blobstore, llm, source, transcriber, demuxer, settings) -> bool:
    """Stage 1: extract, save the text, commit ``stage = embed``. ``False``: a duplicate, done."""
    # ... unchanged: blob_key and adapter checks, ExtractContext, download to a temp file,
    # adapter.extract, the ExtractionTooLarge check, the text hashes, same_text_source ...
    if twin is not None:
        source.duplicate_of_id = twin.id
        await supersede_chunks(session, source)
        log.info("pipeline.duplicate_text", source_id=str(source.id), duplicate_of=str(twin.id))
        _finish(source, chunk_count=0, stage=None)
        await session.commit()
        return False
    source.duplicate_of_id = None
    await blobstore.put(artifact_key(source.id), _pack(units), content_type="application/gzip")
    source.stage = SourceStage.EMBED
    await session.commit()
    return True
```

(`_extract`'s parameters carry the same annotations `run` has.)

```python
async def _embed(
    session: AsyncSession, blobstore: BlobStore, llm: LLMClient, source: Source, settings: Settings
) -> None:
    """Stage 2: chunk the saved text and embed what is not staged yet, committing each window."""
    adapter = select_adapter(source.content_type or "")
    if adapter is None:
        raise UnsupportedContentType(f"no adapter for content type {source.content_type!r}")
    chunks = chunk_units(_unpack(await blobstore.get(artifact_key(source.id))))
    if not chunks:
        raise EmptyExtraction("no text extracted from source")
    if len(chunks) > settings.ingest_max_chunks:
        raise TooManyChunks(
            f"source produced {len(chunks)} chunks, over the "
            f"{settings.ingest_max_chunks} per-job budget"
        )
    staged = set(
        (
            await session.scalars(
                select(StagedChunk.ordinal).where(StagedChunk.source_id == source.id)
            )
        ).all()
    )
    todo = [(ordinal, unit) for ordinal, unit in enumerate(chunks) if ordinal not in staged]
    space = current_space(llm, dim=settings.embed_dim)
    window = settings.embed_batch_size * settings.embed_concurrency
    for start in range(0, len(todo), window):
        part = todo[start : start + window]
        # Every batch is recorded by the client as it completes, on accounting's own
        # transaction, so a partial window's charged batches survive its rollback.
        embedded = await embed_in_batches(
            llm,
            [unit.text for _, unit in part],
            batch_size=settings.embed_batch_size,
            concurrency=settings.embed_concurrency,
        )
        for (ordinal, unit), vector in zip(part, embedded.vectors, strict=True):
            session.add(
                StagedChunk(
                    source_id=source.id,
                    ordinal=ordinal,
                    text=unit.text,
                    embedding=vector,
                    embedding_space=space,
                    pipeline_version=PIPELINE_VERSION,
                    provenance=_provenance(source, unit, adapter.name),
                )
            )
        await session.commit()  # this window survives a failure in the next
```

with `_provenance` holding the dict the old loop built (keep its long comment about measured indicators verbatim):

```python
def _provenance(source: Source, unit: ExtractedUnit, adapter_name: str) -> dict:
    return {
        **unit.locator,
        "source_id": str(source.id),
        "method": unit.method or adapter_name,
        # (the existing comment about measured indicators, unchanged)
        "extraction": extraction_quality.measure(unit.text).model_dump(),
    }
```

```python
async def _publish(session: AsyncSession, source: Source) -> int:
    """Stage 3, one transaction: the staged chunks replace the live ones; the source is done."""
    await supersede_chunks(session, source)  # cited chunks kept as history (S29)
    columns = [
        "id",
        "source_id",
        "ordinal",
        "text",
        "embedding",
        "embedding_space",
        "pipeline_version",
        "provenance",
    ]
    staged = select(
        func.gen_random_uuid(),
        StagedChunk.source_id,
        StagedChunk.ordinal,
        StagedChunk.text,
        StagedChunk.embedding,
        StagedChunk.embedding_space,
        StagedChunk.pipeline_version,
        StagedChunk.provenance,
    ).where(StagedChunk.source_id == source.id)
    count = (await session.execute(insert(Chunk).from_select(columns, staged))).rowcount
    await session.execute(delete(StagedChunk).where(StagedChunk.source_id == source.id))
    _finish(source, chunk_count=count, stage=SourceStage.TAG)
    await session.commit()
    return count


def _finish(source: Source, *, chunk_count: int, stage: str | None) -> None:
    """Done and nobody's job: attempts reset so a pending tag stage has its own tries."""
    source.status = SourceStatus.DONE
    source.stage = stage
    source.error = None
    source.lease_expires_at = None
    source.attempts = 0
    source.meta = {**source.meta, "chunk_count": chunk_count}
```

Rename `_tag_chunks` to `tag_chunks` (update `retag_source`'s call). `run` no longer tags.

- [ ] **Step 4: `ingest_source` and `reset_for_reingest`**

In `app/services/ingestion.py` `ingest_source`, after the `try/except` around `pipeline.run`, replace the block that marks the source `DONE` with:

```python
    await drop_artifact(session, blobstore, source_id)
    return await session.get(Source, source_id, populate_existing=True)
```

and add:

```python
async def drop_artifact(session: AsyncSession, blobstore: BlobStore, source_id: uuid.UUID) -> None:
    """Delete a source's saved extraction (S37); a refusal is retried as a pending erasure."""
    from app.services import retention  # retention imports this module

    key = pipeline.artifact_key(source_id)
    try:
        await blobstore.delete(key)
    except Exception:
        logger.warning("ingest.artifact_not_deleted source=%s", source_id, exc_info=True)
        await retention.queue_erasure(session, ErasureKind.BLOB, key, "refused after publish")
```

(import `ErasureKind` from where `removal.py` imports it.)

In `reset_for_reingest`, before `await session.commit()`:

```python
    # Starts over from extraction (S50): whatever an earlier run staged is discarded, and the
    # saved extraction is overwritten when the new run extracts.
    source.stage = None
    await session.execute(delete(StagedChunk).where(StagedChunk.source_id == source_id))
```

(import `delete` and `StagedChunk`.)

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_ingestion_stages.py tests/test_ingestion_jobs.py tests/test_ingestion_recovery.py tests/test_v0_web_policy.py tests/test_rag.py -q -p no:cacheprovider`
Expected: all pass, except existing tests that assert KC tags straight after `ingest_source` (tagging returns in Task 3) — list them in a ledger ruling; none other may fail.

- [ ] **Step 6: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/rag/pipeline.py app/services/ingestion.py tests/test_ingestion_stages.py
git commit -m "feat(ingestion): extract, embed and publish commit as they go, and resume [S37]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: Tagging as a retried stage

**Files:**
- Modify: `app/learning/kc_tagging.py` (`tag_chunk` lets a refusal through)
- Modify: `app/services/ingestion.py` (tag stage; `claim_source` claims `done`/`tag`; reconcile re-enqueues it)
- Modify: `app/workers/tasks.py` (`_retag_source_task` leaves a refused retag for the sweep)
- Test: `tests/test_ingestion_stages.py` (add)

**Interfaces:**
- Consumes: `pipeline.tag_chunks`, `SourceStage.TAG`, `_finish` leaving `attempts = 0` (Task 2).
- Produces: `claim_source` also claims `status == done AND stage == "tag"` without changing its status; `ingest_source` runs the tag stage after publish and for tag-only claims; log `ingest.tagging_abandoned source=<id>` when a tag stage fails on its last attempt.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingestion_stages.py` (add imports `from datetime import UTC, datetime, timedelta`, `from app.learning.kc_tagging import KCCandidate, tag_chunk`, `from app.models.knowledge import KC, Subject, Topic`, `from app.models.source import ChunkKC`, `import pytest`):

```python
TAGS = '{"tags": [{"kc": 1, "confidence": 0.9}]}'


async def _scoped_source(session: AsyncSession, store: InMemoryBlobStore):
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:6]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    session.add(KC(topic_id=topic.id, slug="k", name="K"))
    await session.flush()
    return await _source(session, store, data=b"Cells respire to release energy.", subject_id=subject.id)


async def _tags(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(ChunkKC)
            .join(Chunk, Chunk.id == ChunkKC.chunk_id)
            .where(Chunk.source_id == source_id)
        )
        or 0
    )


async def test_a_tagging_refusal_is_not_swallowed() -> None:
    llm = _llm(FakeProvider(refuse=ProviderUnavailable("busy", retry_after=5.0)))
    with pytest.raises(ProviderUnavailable):
        await tag_chunk(llm, "text", [KCCandidate(id=uuid.uuid4(), name="K", description=None)])


async def test_a_parse_failure_still_tags_nothing_quietly() -> None:
    tags, _usage = await tag_chunk(
        fake_llm_client("not json"), "text", [KCCandidate(id=uuid.uuid4(), name="K", description=None)]
    )
    assert tags == []


async def test_a_finished_ingest_is_tagged(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)

    done = await ingestion.ingest_source(db_session, store, fake_llm_client(TAGS), source.id)

    assert done is not None and (done.status, done.stage) == (SourceStatus.DONE, None)
    assert await _tags(db_session, source.id) > 0


async def test_a_refused_tag_leaves_the_source_done_and_searchable(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )

    done = await ingestion.ingest_source(db_session, store, refusing, source.id)

    assert done is not None and (done.status, done.stage) == (SourceStatus.DONE, "tag")
    assert done.lease_expires_at is None
    assert await _live_chunks(db_session, source.id)
    assert await _tags(db_session, source.id) == 0


async def test_the_sweep_requeues_a_source_waiting_to_be_tagged(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    await ingestion.ingest_source(db_session, store, refusing, source.id)
    source.updated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    await db_session.commit()
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion.reconcile_stranded(db_session, enqueue, settings=Settings())

    assert source.id in queued


async def test_the_tag_retry_does_not_embed_again(db_session: AsyncSession) -> None:
    store = _CountingStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    await ingestion.ingest_source(db_session, store, refusing, source.id)
    tagger = _FlakyEmbed(fail_on=None)
    tagger._reply = TAGS

    done = await ingestion.ingest_source(db_session, store, _llm(tagger), source.id)

    assert done is not None and (done.status, done.stage) == (SourceStatus.DONE, None)
    assert tagger.embedded == [] and store.downloads == 1
    assert await _tags(db_session, source.id) > 0


async def test_tagging_that_runs_out_of_tries_leaves_the_source_done(
    db_session: AsyncSession,
) -> None:
    store = InMemoryBlobStore()
    source = await _scoped_source(db_session, store)
    refusing = _llm(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"complete"}))
    )
    settings = Settings(ingest_max_attempts=1)
    await ingestion.ingest_source(db_session, store, refusing, source.id, settings=settings)
    again = await ingestion.ingest_source(db_session, store, refusing, source.id, settings=settings)

    assert again is not None
    assert (again.status, again.stage, again.attempts) == (SourceStatus.DONE, "tag", 1)
    assert await ingestion.claim_source(db_session, source.id, settings=settings) is None
```

(`_FlakyEmbed(fail_on=None)` never refuses; setting `_reply` gives it the tagging JSON.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ingestion_stages.py -k "tag" -q -p no:cacheprovider`
Expected: failures — `tag_chunk` swallows the refusal; finished ingests are untagged (`stage` stays `"tag"`); the sweep does not requeue a `done` source.

- [ ] **Step 3: `tag_chunk` lets a refusal through**

In `app/learning/kc_tagging.py` import `CallRefused` from `app.llm.meter` and replace the `except Exception` block:

```python
    except Exception as exc:
        # Best-effort about the *model's answer* — a weak model or a parse failure tags
        # nothing. A refusal is not an answer (S37, S49): it is raised, so the tag stage is
        # retried instead of the source being left untagged for good. DSPy may wrap it.
        refusal = _refusal_in(exc)
        if refusal is not None:
            raise refusal from exc
        return [], lm.usage_sum
```

and add:

```python
def _refusal_in(exc: BaseException) -> CallRefused | None:
    """The refusal ``exc`` is or wraps, if any — DSPy re-raises an LM error in its own type."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, CallRefused):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None
```

Update its docstring: "Best-effort: no candidates ⇒ no call; a DSPy/parse failure ⇒ no tags. A refused call (spend or provider) is raised."

- [ ] **Step 4: The tag stage, claiming it, sweeping it**

In `app/services/ingestion.py`:

`claim_source` — the claimable condition gains a third branch and the status is kept for it:

```python
            or_(
                Source.status == SourceStatus.PENDING,
                and_(
                    Source.status == SourceStatus.PROCESSING,
                    Source.lease_expires_at < func.now(),
                ),
                # Published, concept tags still to write (S37): searchable already, so it stays
                # DONE while the tag stage runs.
                and_(Source.status == SourceStatus.DONE, Source.stage == SourceStage.TAG),
            ),
```

and in `.values(...)`: `status=case((Source.status == SourceStatus.DONE, SourceStatus.DONE), else_=SourceStatus.PROCESSING),` (import `case`). Update the docstring's "Claimable means …" sentence to include the tag stage.

`ingest_source` — run the pipeline only when the claim is not tag-only, then the tag stage:

```python
    if source.status != SourceStatus.DONE:  # a tag-only claim skips straight to tagging
        try:
            ...  # the existing _require_file_source + timeout + pipeline.run block
        except Exception as exc:
            await session.rollback()  # discard this stage's uncommitted writes
            return await _record_failure(session, source_id, exc, settings=settings)
        await drop_artifact(session, blobstore, source_id)
        source = await session.get(Source, source_id, populate_existing=True)
        if source is None or source.stage != SourceStage.TAG:
            return source  # a same-text duplicate has nothing to tag
    return await _tag_stage(session, llm, source, settings)
```

```python
async def _tag_stage(
    session: AsyncSession, llm: LLMClient, source: Source, settings: Settings
) -> Source:
    """Stage 4 (S37): write concept tags. A failure leaves the source DONE, searchable, and
    ``stage = tag`` for the reconcile sweep; the last allowed attempt gives up once, loudly."""
    source_id = source.id
    try:
        await pipeline.retag_source(session, llm, source, settings=settings)
        source.stage = None
    except Exception:
        await session.rollback()
        refreshed = await session.get(Source, source_id, populate_existing=True)
        assert refreshed is not None
        source = refreshed
        if source.attempts >= settings.ingest_max_attempts:
            logger.warning("ingest.tagging_abandoned source=%s", source_id)
        else:
            logger.info("ingest.tagging_deferred source=%s", source_id, exc_info=True)
    source.lease_expires_at = None
    await session.commit()
    return source
```

`retag_source` must not also be the thing that commits half a retag: it deletes old tags, tags, commits — a failure before its commit is rolled back above, which is what we want.

`reconcile_stranded` — the `stranded` query's `or_(...)` gains:

```python
                    and_(
                        Source.status == SourceStatus.DONE,
                        Source.stage == SourceStage.TAG,
                        Source.updated_at < func.now() - grace,
                    ),
```

(The `exhausted` UPDATE is unchanged: it only parks `pending`/`processing` sources, so a source that ran out of tag tries stays `done`.)

In `app/workers/tasks.py` `_retag_source_task`, wrap the retag:

```python
        try:
            await pipeline.retag_source(session, llm, source, settings=settings)
        except CallRefused as exc:
            # Deferred, not dropped (S37): the sweep retries a source waiting to be tagged.
            await session.rollback()
            source = await session.get(Source, uuid.UUID(source_id), populate_existing=True)
            if source is not None:
                source.stage = SourceStage.TAG
                source.attempts = 0
                await session.commit()
            logger.info("%s.deferred task=retag_source", exc.reason)
```

(import `SourceStage`.)

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_ingestion_stages.py tests/test_ingestion_jobs.py tests/test_ingestion_recovery.py tests/test_source_scope.py tests/test_rag.py -q -p no:cacheprovider`
Expected: all pass (including any tagging tests Task 2 ledgered as failing).

- [ ] **Step 6: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/learning/kc_tagging.py app/services/ingestion.py app/workers/tasks.py tests/test_ingestion_stages.py
git commit -m "feat(ingestion): concept tagging is a stage that a refusal leaves for retry [S37]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: Exact slots, a per-learner cap, and starting the next upload

**Files:**
- Create: `app/services/ingest_slots.py`
- Modify: `app/services/ingestion.py` (`ingest_source` takes slots, removes the soft cap, dispatches the next source), `app/workers/tasks.py` (passes `enqueue`)
- Modify: `tests/test_ingestion_jobs.py` (replace `test_the_concurrency_cap_refuses_a_further_claim`)
- Test: `tests/test_ingest_slots.py` (create)

**Interfaces:**
- Produces: `ingest_slots.take(engine: AsyncEngine, learner_id: uuid.UUID, settings: Settings) -> IngestSlots | None`; `IngestSlots.release() -> None` (idempotent; unlocks all and closes). `ingest_source(..., enqueue: Callable[[uuid.UUID], Awaitable[None]] | None = None)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest_slots.py`:

```python
"""Exact ingestion slots, global and per learner, held as advisory locks (S37)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import SourceKind, SourceStatus
from app.services import ingest_slots, ingestion
from app.storage import InMemoryBlobStore


async def test_the_global_slots_are_exact(engine: AsyncEngine) -> None:
    settings = Settings(ingest_max_concurrent_jobs=2, ingest_max_jobs_per_learner=4)
    held = [await ingest_slots.take(engine, uuid.uuid4(), settings) for _ in range(2)]
    try:
        assert all(held)
        assert await ingest_slots.take(engine, uuid.uuid4(), settings) is None
    finally:
        for slots in held:
            if slots is not None:
                await slots.release()


async def test_one_learner_cannot_take_every_slot(engine: AsyncEngine) -> None:
    settings = Settings(ingest_max_concurrent_jobs=4, ingest_max_jobs_per_learner=1)
    learner = uuid.uuid4()
    first = await ingest_slots.take(engine, learner, settings)
    other = await ingest_slots.take(engine, uuid.uuid4(), settings)
    try:
        assert first is not None and other is not None
        assert await ingest_slots.take(engine, learner, settings) is None
    finally:
        for slots in (first, other):
            if slots is not None:
                await slots.release()


async def test_released_slots_can_be_taken_again(engine: AsyncEngine) -> None:
    """The pool reuses connections, and a session lock survives close() — release unlocks."""
    settings = Settings(ingest_max_concurrent_jobs=1)
    first = await ingest_slots.take(engine, uuid.uuid4(), settings)
    assert first is not None
    await first.release()
    await first.release()  # idempotent

    again = await ingest_slots.take(engine, uuid.uuid4(), settings)
    assert again is not None
    await again.release()


async def _source(session: AsyncSession, store: InMemoryBlobStore, learner: Learner):
    return await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex}.txt",
        content_type="text/plain",
        data=uuid.uuid4().hex.encode() + b" The cell releases energy.",
    )


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"q-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_ingest_waits_when_no_slot_is_free(
    db_session: AsyncSession, engine: AsyncEngine
) -> None:
    store = InMemoryBlobStore()
    learner = await _learner(db_session)
    source = await _source(db_session, store, learner)
    settings = Settings(ingest_max_concurrent_jobs=1)
    busy = await ingest_slots.take(engine, uuid.uuid4(), settings)
    try:
        result = await ingestion.ingest_source(
            db_session, store, fake_llm_client(), source.id, settings=settings
        )
    finally:
        assert busy is not None
        await busy.release()

    assert result is None
    await db_session.refresh(source)
    assert (source.status, source.attempts) == (SourceStatus.PENDING, 0)


async def test_ingest_releases_its_slots_even_when_the_job_fails(
    db_session: AsyncSession, engine: AsyncEngine, monkeypatch
) -> None:
    store = InMemoryBlobStore()
    learner = await _learner(db_session)
    source = await _source(db_session, store, learner)
    settings = Settings(ingest_max_concurrent_jobs=1)

    async def boom(*args, **kwargs):
        raise ConnectionError("provider unreachable")

    monkeypatch.setattr("app.rag.pipeline.run", boom)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id, settings=settings)

    slots = await ingest_slots.take(engine, uuid.uuid4(), settings)
    assert slots is not None
    await slots.release()


async def test_a_finished_job_starts_the_next_waiting_upload(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = await _learner(db_session)
    first = await _source(db_session, store, learner)
    waiting = await _source(db_session, store, learner)
    await db_session.commit()
    queued: list[uuid.UUID] = []

    async def enqueue(source_id: uuid.UUID) -> None:
        queued.append(source_id)

    await ingestion.ingest_source(
        db_session, store, fake_llm_client(), first.id, enqueue=enqueue
    )

    assert queued == [waiting.id]
```

In `tests/test_ingestion_jobs.py`, delete `test_the_concurrency_cap_refuses_a_further_claim` (the cap now lives in the slots, tested above).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ingest_slots.py -q -p no:cacheprovider`
Expected: collection error — `cannot import name 'ingest_slots'`.

- [ ] **Step 3: Implement the slots**

Create `app/services/ingest_slots.py`:

```python
"""Exact ingestion slots, global and per learner (S37).

A claim used to count live jobs in a subquery of its own UPDATE — tighter than read-then-write,
but under READ COMMITTED two racing claims could both see room and both take it. A slot is a
Postgres session-level advisory lock on a connection held for the job, the ``turn_lock``
pattern: exact under any race, and freed by Postgres when a crashed worker's connection ends,
so it needs no timeout. A job takes one global slot of ``ingest_max_concurrent_jobs`` and one
of its learner's ``ingest_max_jobs_per_learner``, so one learner's pile of uploads cannot take
every slot.

Released with ``pg_advisory_unlock_all()`` before the connection goes back to the pool: the
pool reuses connections, and a session lock outlives ``close()`` on a pooled one.
"""

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings
from app.services.turn_lock import _as_int4

log = structlog.get_logger(__name__)

GLOBAL_NAMESPACE = 0x494E4753  # "INGS"
LEARNER_NAMESPACE = 0x494E474C  # "INGL"


def learner_key(learner_id: uuid.UUID, slot: int) -> int:
    """The learner's ``slot``-th lock id: 28 bits of the learner, 4 bits of slot (≤ 16)."""
    return ((learner_id.int & 0x0FFFFFFF) << 4) | slot


@dataclass
class IngestSlots:
    """A held pair of slots. :meth:`release` is idempotent."""

    connection: AsyncConnection
    _released: bool = field(default=False, repr=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self.connection.execute(text("SELECT pg_advisory_unlock_all()"))
        except Exception:
            log.warning("ingest_slots.unlock_failed")
        finally:
            await self.connection.close()


async def _try(connection: AsyncConnection, namespace: int, key: int) -> bool:
    return bool(
        await connection.scalar(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": _as_int4(namespace), "objid": _as_int4(key)},
        )
    )


async def take(engine: AsyncEngine, learner_id: uuid.UUID, settings: Settings) -> IngestSlots | None:
    """A global and a learner slot, or ``None`` when either kind is full."""
    connection = await engine.connect()
    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
    slots = IngestSlots(connection)
    try:
        for i in range(settings.ingest_max_concurrent_jobs):
            if await _try(connection, GLOBAL_NAMESPACE, i):
                break
        else:
            await slots.release()
            return None
        for j in range(settings.ingest_max_jobs_per_learner):
            if await _try(connection, LEARNER_NAMESPACE, learner_key(learner_id, j)):
                return slots
        await slots.release()
        return None
    except BaseException:
        await slots.release()
        raise
```

- [ ] **Step 4: `ingest_source` takes slots and starts the next upload**

In `app/services/ingestion.py`:

- In `claim_source`, remove the `live_jobs` subquery and its condition; replace the docstring's paragraph about the soft cap with: "The concurrency ceiling is not here: ``ingest_source`` takes exact slots (``app/services/ingest_slots.py``) before claiming."
- `ingest_source` gains `enqueue: Callable[[uuid.UUID], Awaitable[None]] | None = None` and becomes:

```python
    settings = settings or get_settings()
    learner_id = await session.scalar(select(Source.learner_id).where(Source.id == source_id))
    if learner_id is None:
        return None
    slots = await ingest_slots.take(_engine_of(session), learner_id, settings)
    if slots is None:
        return None  # full: the source stays pending; a finishing job or the sweep starts it
    try:
        return await _ingest_claimed(
            session, blobstore, llm, source_id, transcriber, demuxer, settings
        )
    finally:
        await slots.release()
        if enqueue is not None:
            await _start_next(session, enqueue, learner_id, settings)
```

where `_ingest_claimed` is the previous body from `claim_source(...)` onward (Task 2 and 3 versions), and:

```python
def _engine_of(session: AsyncSession) -> AsyncEngine:
    """The engine behind ``session`` — slots need their own connection to the same database."""
    bind = session.bind
    return bind if isinstance(bind, AsyncEngine) else cast(AsyncConnection, bind).engine


async def _start_next(
    session: AsyncSession,
    enqueue: Callable[[uuid.UUID], Awaitable[None]],
    learner_id: uuid.UUID,
    settings: Settings,
) -> None:
    """Dispatch the oldest waiting upload — this learner's first — now that a slot is free."""
    try:
        next_id = await session.scalar(
            select(Source.id)
            .where(
                Source.status == SourceStatus.PENDING,
                Source.attempts < settings.ingest_max_attempts,
                Source.kind == SourceKind.FILE,
            )
            .order_by((Source.learner_id != learner_id), Source.created_at, Source.id)
            .limit(1)
        )
        if next_id is not None:
            await enqueue(next_id)
    except Exception:
        # The sweep finds it anyway; a missed dispatch only costs a wait.
        logger.warning("ingest.next_not_dispatched", exc_info=True)
```

(imports: `ingest_slots`, `AsyncEngine`, `AsyncConnection`, `cast`.)

In `app/workers/tasks.py` `_ingest_source_task`, pass `enqueue=_enqueue_ingestion` to `ingestion.ingest_source` (define-order permitting — `_enqueue_ingestion` is a module-level function, so it resolves at call time).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_ingest_slots.py tests/test_ingestion_jobs.py tests/test_ingestion_stages.py tests/test_ingestion_recovery.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/services/ingest_slots.py app/services/ingestion.py app/workers/tasks.py tests/test_ingest_slots.py tests/test_ingestion_jobs.py
git commit -m "feat(ingestion): exact job slots, a per-learner cap, and the next upload starts at once [S37]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: Deleting a source or an account removes its saved extraction

**Files:**
- Modify: `app/services/removal.py` (`delete_source`), `app/services/retention.py` (`delete_learner`)
- Test: `tests/test_ingestion_stages.py` (add)

**Interfaces:**
- Consumes: `pipeline.artifact_key`, `ingestion.drop_artifact` (Task 2); `StagedChunk` cascade (Task 1).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingestion_stages.py`:

```python
async def test_deleting_a_half_ingested_source_removes_what_it_saved(
    db_session: AsyncSession,
) -> None:
    from app.services import removal

    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=2)), source.id, settings=SETTINGS
    )
    assert await store.exists(pipeline.artifact_key(source.id))

    await removal.delete_source(db_session, store, source.learner_id, source.id, forget=False)

    assert not await store.exists(pipeline.artifact_key(source.id))
    assert await _staged(db_session, source.id) == 0


async def test_erasing_an_account_removes_a_half_ingested_extraction(
    db_session: AsyncSession,
) -> None:
    from app.services import retention

    store = InMemoryBlobStore()
    source = await _source(db_session, store)
    await ingestion.ingest_source(
        db_session, store, _llm(_FlakyEmbed(fail_on=2)), source.id, settings=SETTINGS
    )

    report = await retention.delete_learner(db_session, store, source.learner_id)

    assert not await store.exists(pipeline.artifact_key(source.id))
    assert report.blobs_failed == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_ingestion_stages.py -k "deleting or erasing" -q -p no:cacheprovider`
Expected: both fail — the artifact is still in the store.

- [ ] **Step 3: Implement**

`app/services/removal.py` `delete_source`, after the blob block (still inside the function, after the `if blob_key is not None:` block):

```python
    # A half-ingested source's saved extraction (S37). Deleting a missing key is not an error.
    await ingestion.drop_artifact(session, blobstore, source_id)
```

`app/services/retention.py` `delete_learner`: collect the learner's source ids beside `blob_keys` (before anything is deleted):

```python
    source_ids = list(
        (await session.scalars(select(Source.id).where(Source.learner_id == learner_id))).all()
    )
```

and after the `for key in blob_keys:` loop:

```python
    for source_id in source_ids:
        # Saved extractions of half-ingested sources (S37): per source, never shared.
        key = pipeline.artifact_key(source_id)
        try:
            await blobstore.delete(key)
        except Exception:
            report.blobs_failed.append(key)
```

(import `pipeline` from `app.rag`.) A refused key joins `blobs_failed`, which `erase_learner` already queues as a pending erasure; its retry calls `unreference_blob`, which deletes it because no source names it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_ingestion_stages.py tests/test_removal.py tests/test_retention.py tests/test_account_deletion.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/services/removal.py app/services/retention.py tests/test_ingestion_stages.py
git commit -m "feat(ingestion): deleting a source or an account removes its saved extraction [S37]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 6: Record it

**Files:** `docs/guru-suggestions-tracker.md`, `docs/RUNBOOK.md` (§5), `CLAUDE.md`

- [ ] **Step 1: Tracker**
  - Remove the `S37` row from the Workstream 5 live table; add to Completed after `S36` (or in id order):
    `| S37 | Bounded, resumable ingestion | Extraction, embedding and publish commit as stages and resume where they stopped (no OCR or embedding paid twice); concept tagging is a retried stage a refusal cannot silently skip; exact global slots and a per-learner cap (2) as advisory locks; a finishing job starts the next waiting upload. | Per-job spend ceiling (deferred until daily caps prove too coarse); S18 (`ingest_max_jobs_per_learner`) | [Stages](../app/rag/pipeline.py), [slots](../app/services/ingest_slots.py), [design](superpowers/specs/2026-09-30-resumable-ingestion-design.md) |`
  - At a glance: Live v0 count − 1 and drop `S37`; Done + 1; "Next up" becomes `**Next up: workstream 5** — S17, then S62, S53.`
  - On the S49 Completed row, delete the hand-off clause "S37 (KC tagging swallows an outage, leaving a source untagged)."
  - S18's Remaining: after "the provider retry wait" add ", the per-learner ingestion cap".

- [ ] **Step 2: RUNBOOK §5 (Ingesting content)** — add a subsection:

```markdown
### Stages, slots and resuming (S37)

An ingestion job runs in stages that each commit, recorded in `sources.stage`:
extract (text saved to `ingest/{source_id}/extract.json.gz` in the object store) → embed
(chunks in `staged_chunks`, committed per window of `GURU_EMBED_BATCH_SIZE ×
GURU_EMBED_CONCURRENCY`) → publish (one transaction: old chunks superseded, cited ones kept;
staged rows become live; the source is `done`) → tag (concept tags). A retry or a reclaimed
lease resumes at the recorded stage, so OCR, transcripts and embeddings are never paid twice;
nothing reads `staged_chunks`, so retrieval never sees half a source. A refused tag stage
leaves the source `done` and searchable with `stage = 'tag'`; the reconcile sweep retries it
up to `GURU_INGEST_MAX_ATTEMPTS`, then logs `ingest.tagging_abandoned`. A job takes an exact
global slot (`GURU_INGEST_MAX_CONCURRENT_JOBS`, 4) and one of its learner's
(`GURU_INGEST_MAX_JOBS_PER_LEARNER`, 2) as advisory locks, or leaves the source `pending`; a
finishing job dispatches the next waiting upload. Deleting a source or an account removes its
saved extraction and staged rows.
```

- [ ] **Step 3: CLAUDE.md** — in the "A citation outlives a re-ingest" bullet, append: "Ingestion runs in committed stages (extract → embed → publish → tag) that resume where they stopped, under exact global and per-learner slots (S37)."

- [ ] **Step 4: Commit and gate**

```bash
git add docs/guru-suggestions-tracker.md docs/RUNBOOK.md CLAUDE.md
git commit -m "docs: record resumable, strictly bounded ingestion [S37]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
uv run poe check && uv run poe format-check
```

Expected: all pass.
