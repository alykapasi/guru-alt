# Versioned Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-ingesting a source keeps what old citations point at, needs the learner's confirmation when the source is already done, and an operator command brings stale sources up to date resumably.

**Architecture:** Chunks gain `superseded_at` and `pipeline_version`; `pipeline.run` supersedes instead of deleting (keeping only cited chunks, without vectors). The retry endpoint requires `confirm` for DONE sources. `app/services/reindex.py` derives staleness from the data (embedding space, pipeline version, scope consistency) and `poe reindex` shows or applies it.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, pgvector, pytest; React + TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-09-26-versioned-sources-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract` (stage `frontend/src/api/schema.d.ts` after `uv run poe api-types` when the API shape changes — the contract check compares against the index).
- Frontend changes also green on `cd frontend && npm run build` and `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`.
- A new migration needs the test database upgraded: `uv run python -m tests.testdb`.
- A new route taking a graph id must be added to `tests/test_visibility_sweep.py` `CASES` (none expected in this plan).
- One tracker id per commit subject: `[S29]` or `[S50]` as each task states.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; run `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- Never print `.env` or secrets. No paid model calls: tests use `fake_llm_client`.

## Review Focus

1. A conversation deleted after citing a chunk: its messages go with it, so the next re-ingest deletes the chunk — citations are read from existing rows, never remembered (no separate test; the query reads live rows only).
2. Re-ingesting twice: the first run's superseded chunks must survive the second run untouched (Task 2 test).
3. Retry with an empty JSON body or no body at all must behave as `confirm: false` — not a 422 (Task 3 test).
4. A reindex run interrupted halfway leaves already-done sources current; running again only touches the rest (Task 4 test: second run finds nothing).
5. A source whose chunks are all superseded (only history left) is neither re-embedded nor re-extracted (Task 4 test).

---

### Task 1: Chunk versions, superseded state, and current-only readers [S29]

**Files:**
- Create: `db/migrations/versions/0064_chunk_versions.py`
- Modify: `app/models/source.py` (class `Chunk`), `app/rag/pipeline.py` (`PIPELINE_VERSION`, stamp in `run`, current-only in `retag_source`), `app/rag/retrieval.py` (`scoped`), `app/api/v1/sources.py` (`get_source_chunks`), `app/schemas/source.py` (`ChunkRead`)
- Test: `tests/test_chunk_versions.py` (create), `tests/test_migrations_with_data.py`

**Interfaces:**
- Produces: `Chunk.superseded_at: datetime | None`, `Chunk.pipeline_version: int`, `Chunk.embedding` nullable; `PIPELINE_VERSION: int = 1` in `app.rag.pipeline`; `ChunkRead.superseded: bool`.

- [ ] **Step 1: Write the failing tests** — `tests/test_chunk_versions.py`:

```python
"""Chunks carry the pipeline version that wrote them, and superseded ones stay out of reach (S29)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk
from app.rag import pipeline, retrieval
from app.rag.scope import SourceScope
from app.services import ingestion
from app.storage import InMemoryBlobStore
from sqlalchemy import select

TEXT = b"Photosynthesis converts light energy into chemical energy in chloroplasts."


async def _ingested(session: AsyncSession, learner: Learner) -> list[Chunk]:
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=ingestion.SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=TEXT,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return list(
        (await session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    )


async def test_new_chunks_carry_the_current_pipeline_version(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    chunks = await _ingested(db_session, api_learner)
    assert chunks
    assert {c.pipeline_version for c in chunks} == {pipeline.PIPELINE_VERSION}
    assert all(c.superseded_at is None for c in chunks)


async def test_retrieval_never_returns_a_superseded_chunk(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    [chunk, *_] = await _ingested(db_session, api_learner)
    chunk.superseded_at = datetime.now(UTC)
    chunk.embedding = None
    await db_session.flush()

    hits = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "photosynthesis chloroplasts",
        scope=SourceScope(learner_id=api_learner.id),
    )

    assert chunk.id not in {h.chunk_id for h in hits}


async def test_the_chunk_endpoint_marks_a_superseded_chunk(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    [chunk, *_] = await _ingested(db_session, api_learner)
    chunk.superseded_at = datetime.now(UTC)
    chunk.embedding = None
    await db_session.commit()

    body = (await api_client.get(f"/api/v1/chunks/{chunk.id}")).json()
    listing = (await api_client.get(f"/api/v1/sources/{chunk.source_id}/chunks")).json()

    assert body["superseded"] is True
    assert body["text"] == chunk.text
    assert chunk.id not in {uuid.UUID(c["id"]) for c in listing}, "the source's listing is current"
```

If `ingestion.SourceKind` is not re-exported, import `SourceKind` from `app.models.source` instead.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_chunk_versions.py -v`
Expected: FAIL — `AttributeError: module 'app.rag.pipeline' has no attribute 'PIPELINE_VERSION'`.

- [ ] **Step 3: Migration** `db/migrations/versions/0064_chunk_versions.py`:

```python
"""Chunk versions: which pipeline wrote a chunk, and whether a newer one replaced it (S29, S50).

Every existing chunk was written by the only pipeline there has been, so version 1 is a fact
about it, not a guess — a server default is right here. ``superseded_at`` starts NULL: nothing
has been replaced yet. ``embedding`` becomes nullable because a superseded chunk keeps its text
for the citations that point at it and gives up the vector nobody will search again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_chunk_versions"
down_revision: str | Sequence[str] | None = "0063_source_scope_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column("pipeline_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("chunks", "embedding", nullable=True)


def downgrade() -> None:
    # A superseded chunk has no vector; it cannot satisfy NOT NULL again, and it is history
    # nothing can search, so it goes.
    op.execute("DELETE FROM chunks WHERE embedding IS NULL")
    op.alter_column("chunks", "embedding", nullable=False)
    op.drop_column("chunks", "pipeline_version")
    op.drop_column("chunks", "superseded_at")
```

Run `uv run python -m tests.testdb` afterwards.

- [ ] **Step 4: Model.** In `app/models/source.py`, class `Chunk`: change `embedding` to

```python
    # NULL only on a superseded chunk (S29): it keeps its text for the citations that point at
    # it and gives up the vector, since nothing searches superseded chunks.
    embedding: Mapped[Any | None] = mapped_column(Vector(_EMBED_DIM), nullable=True)
```

and add after `provenance`:

```python
    # The extraction/chunking version that wrote this chunk (``app.rag.pipeline.PIPELINE_VERSION``).
    # Lower than the current version means the text itself may now come out differently, and
    # only a re-extraction can bring it up to date (S50).
    pipeline_version: Mapped[int] = mapped_column(server_default="1", default=1)
    # Set when a re-ingest replaced this chunk but something still cites it (S29). A superseded
    # chunk is history: readable through its citation, never retrieved, tagged or re-embedded.
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

(add `datetime` / `DateTime` imports as needed).

- [ ] **Step 5: Pipeline version and current-only readers.**
  - `app/rag/pipeline.py`, after the imports:

```python
# Bumped whenever extraction or chunking changes in a way that changes chunk text (S50).
# `poe reindex` compares every current chunk against it; see docs/RUNBOOK.md §15.
PIPELINE_VERSION = 1
```

  and in `run`, add `pipeline_version=PIPELINE_VERSION,` to the `Chunk(...)` constructor.
  - `retag_source`: `select(Chunk).where(Chunk.source_id == source.id, Chunk.superseded_at.is_(None))`.
  - `app/rag/retrieval.py` `scoped()`: first line after the join becomes `.where(Source.learner_id == scope.learner_id, Chunk.superseded_at.is_(None))`.
  - `app/api/v1/sources.py` `get_source_chunks`: add `Chunk.superseded_at.is_(None)` to the `where`.
  - `app/schemas/source.py` `ChunkRead`: add

```python
    # A re-ingest replaced this passage but a citation still points at it (S29).
    superseded_at: datetime | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def superseded(self) -> bool:
        return self.superseded_at is not None
```

  (import `datetime`, `Field`, `computed_field`).

- [ ] **Step 6: Migration data test.** Append to `tests/test_migrations_with_data.py`:

```python
async def test_chunks_that_predate_versions_come_through_as_version_one_and_current() -> None:
    """0064 (S29/S50): every existing chunk was written by pipeline 1, and none is superseded."""
    async with database_at("0063_source_scope_settings") as connect:
        conn = await connect()
        try:
            learner_id, source_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            dim = int(await conn.fetchval(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            ))
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "reader"
            )
            await conn.execute(
                "INSERT INTO sources (id, learner_id, kind, origin, status, meta) "
                "VALUES ($1, $2, 'file', 'notes.txt', 'done', '{}'::jsonb)",
                source_id,
                learner_id,
            )
            await conn.execute(
                "INSERT INTO chunks (id, source_id, ordinal, text, embedding, embedding_space, "
                "provenance) VALUES ($1, $2, 0, 'old text', $3::vector, 'fake:fake-1:x', "
                "'{}'::jsonb)",
                chunk_id,
                source_id,
                "[" + ",".join(["0.1"] * dim) + "]",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0064_chunk_versions")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT pipeline_version, superseded_at, text FROM chunks WHERE id = $1",
                chunk_id,
            )
            assert row is not None
            assert row["pipeline_version"] == 1
            assert row["superseded_at"] is None
            assert row["text"] == "old text"
        finally:
            await conn.close()
```

If the `sources` insert fails on another NOT NULL column, add that column with a plausible literal — read the error, do not guess the schema. If the vector dimension query returns a typmod that is not the dimension, use `get_settings().embed_dim` instead.

- [ ] **Step 7: Run tests and the gate**

Run: `uv run pytest tests/test_chunk_versions.py tests/test_migrations_with_data.py tests/test_retrieval.py -v` → PASS.
Run: `uv run poe api-types`, then `uv run poe check && uv run poe format-check`; stage and run `uv run poe api-contract` → green.

- [ ] **Step 8: Commit**

```bash
git add db/migrations/versions/0064_chunk_versions.py app/models/source.py app/rag/pipeline.py app/rag/retrieval.py app/api/v1/sources.py app/schemas/source.py frontend/src/api/schema.d.ts tests/test_chunk_versions.py tests/test_migrations_with_data.py
git status
git commit -m "feat(rag): chunk versions and superseded chunks kept out of reach [S29]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Re-ingest supersedes cited chunks instead of deleting them [S29]

**Files:**
- Modify: `app/rag/pipeline.py` (new `supersede_chunks`, used in `run`)
- Test: `tests/test_supersede.py` (create)

**Interfaces:**
- Consumes: `Chunk.superseded_at`, `Chunk.embedding` nullable (Task 1).
- Produces: `async def supersede_chunks(session: AsyncSession, source: Source) -> tuple[int, int]` returning `(kept, deleted)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_supersede.py`:

```python
"""Re-ingesting a source keeps what old citations point at (S29)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.content import ContentBlock
from app.models.learner import Learner
from app.models.source import Chunk, ChunkKC, Source, SourceKind
from app.services import content as content_svc
from app.services import ingestion
from app.storage import InMemoryBlobStore

TEXT = b"Photosynthesis converts light energy into chemical energy in chloroplasts."


async def _ingest(session: AsyncSession, store: InMemoryBlobStore, learner: Learner) -> Source:
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=TEXT,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return source


async def _chunks(session: AsyncSession, source: Source) -> list[Chunk]:
    session.expire_all()
    return list(
        (
            await session.scalars(
                select(Chunk).where(Chunk.source_id == source.id).order_by(Chunk.superseded_at)
            )
        ).all()
    )


async def _cite_in_message(session: AsyncSession, learner: Learner, chunk: Chunk) -> None:
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="as shown [1]",
            citations=[{"marker": 1, "chunk_id": str(chunk.id), "source_id": str(chunk.source_id)}],
        )
    )
    await session.commit()


async def _reingest(session: AsyncSession, store: InMemoryBlobStore, source: Source) -> None:
    await ingestion.reset_for_reingest(session, source.id)
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)


async def test_a_cited_chunk_survives_reingest_as_superseded(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    await _cite_in_message(db_session, api_learner, original)

    await _reingest(db_session, store, source)

    chunks = await _chunks(db_session, source)
    old = next(c for c in chunks if c.id == original.id)
    current = [c for c in chunks if c.superseded_at is None]
    assert old.superseded_at is not None
    assert old.text == original.text
    assert old.embedding is None
    assert len(current) == 1 and current[0].id != original.id
    tags = await db_session.scalars(select(ChunkKC).where(ChunkKC.chunk_id == old.id))
    assert list(tags) == []


async def test_an_uncited_chunk_is_deleted_on_reingest(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)

    await _reingest(db_session, store, source)

    assert original.id not in {c.id for c in await _chunks(db_session, source)}


async def test_a_lesson_block_citation_also_keeps_the_chunk(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    db_session.add(
        ContentBlock(
            learner_id=api_learner.id,
            kc_ids=[],
            block_type="lesson",
            body="x",
            citations=[{"chunk_id": str(original.id), "source_id": str(source.id)}],
            cache_key=f"k-{uuid.uuid4().hex}",
            model="fake-1",
        )
    )
    await db_session.commit()

    await _reingest(db_session, store, source)

    assert original.id in {c.id for c in await _chunks(db_session, source)}


async def test_a_second_reingest_leaves_earlier_history_alone(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    await _cite_in_message(db_session, api_learner, original)

    await _reingest(db_session, store, source)
    await _reingest(db_session, store, source)

    chunks = await _chunks(db_session, source)
    assert original.id in {c.id for c in chunks}
    assert len([c for c in chunks if c.superseded_at is None]) == 1


async def test_deleting_the_source_takes_its_history_with_it(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    await _cite_in_message(db_session, api_learner, original)
    await _reingest(db_session, store, source)

    await db_session.delete(await db_session.get(Source, source.id))
    await db_session.commit()

    assert await db_session.get(Chunk, original.id) is None


async def test_the_support_checker_still_reads_a_superseded_citation(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _ingest(db_session, store, api_learner)
    [original] = await _chunks(db_session, source)
    block = ContentBlock(
        learner_id=api_learner.id,
        kc_ids=[],
        block_type="lesson",
        body="Photosynthesis happens in chloroplasts.",
        citations=[{"chunk_id": str(original.id), "source_id": str(source.id)}],
        cache_key=f"k-{uuid.uuid4().hex}",
        model="fake-1",
    )
    db_session.add(block)
    await db_session.commit()
    await _reingest(db_session, store, source)

    passages = await content_svc._cited_passages(db_session, block)

    assert [p.id for p in passages] == [original.id]
```

`content_svc._cited_passages` is the name to give the existing chunk-loading lines in `check_block_citations` (`app/services/content.py` around line 228: `select(Chunk).where(Chunk.id.in_(ids))`) if they are not already a function: extract them into `async def _cited_passages(session: AsyncSession, block: ContentBlock) -> list[Chunk]` with the same query and ordering, and call it from `check_block_citations`. If a helper already exists under another name, use that name in the test instead.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_supersede.py -v`
Expected: FAIL — the cited chunk is gone after re-ingest (`StopIteration` / `assert … in …`).

- [ ] **Step 3: Implement** in `app/rag/pipeline.py` (add `text`, `bindparam`, `func`, `update` from sqlalchemy and `ARRAY`, `String` as needed):

```python
_CITED = text(
    """
    SELECT c->>'chunk_id' FROM messages m
      JOIN conversations v ON v.id = m.conversation_id AND v.learner_id = :learner
      CROSS JOIN LATERAL jsonb_array_elements(m.citations) AS c
     WHERE c->>'chunk_id' = ANY(:ids)
    UNION
    SELECT c->>'chunk_id' FROM content_blocks b
      CROSS JOIN LATERAL jsonb_array_elements(b.citations) AS c
     WHERE b.learner_id = :learner AND c->>'chunk_id' = ANY(:ids)
    """
).bindparams(bindparam("ids", type_=ARRAY(String)))


async def supersede_chunks(session: AsyncSession, source: Source) -> tuple[int, int]:
    """Retire a source's current chunks before new ones are written (S29). Returns (kept, deleted).

    A chunk something cites — a chat reply or a lesson block, only ever the owner's — is kept as
    history: its text and locator stay so the citation still shows what it cited, while its
    vector and concept tags go, because nothing will search or tag it again. Everything else is
    deleted as before. Chunks superseded by an earlier re-ingest are left exactly as they are.
    """
    current = list(
        (
            await session.scalars(
                select(Chunk.id).where(
                    Chunk.source_id == source.id, Chunk.superseded_at.is_(None)
                )
            )
        ).all()
    )
    if not current:
        return 0, 0
    rows = await session.execute(
        _CITED, {"learner": source.learner_id, "ids": [str(i) for i in current]}
    )
    cited = {uuid.UUID(r[0]) for r in rows}
    uncited = [i for i in current if i not in cited]
    if uncited:
        await session.execute(delete(Chunk).where(Chunk.id.in_(uncited)))
    if cited:
        await session.execute(delete(ChunkKC).where(ChunkKC.chunk_id.in_(cited)))
        await session.execute(
            update(Chunk)
            .where(Chunk.id.in_(cited))
            .values(superseded_at=func.now(), embedding=None)
        )
    return len(cited), len(uncited)
```

and in `run`, replace

```python
    # Idempotent: replace any prior chunks for this source (their KC tags cascade away with them).
    await session.execute(delete(Chunk).where(Chunk.source_id == source.id))
```

with

```python
    # Replace the prior chunks, keeping any a citation still points at (S29).
    await supersede_chunks(session, source)
```

(`import uuid` at the top if not present.)

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_supersede.py tests/test_chunk_versions.py tests/test_ingestion_recovery.py tests/test_content.py tests/test_citation_support.py -v` → PASS.
Run: `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/rag/pipeline.py app/services/content.py tests/test_supersede.py
git status
git commit -m "feat(rag): re-ingest keeps cited chunks as history instead of deleting them [S29]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Re-processing a finished source asks first [S29]

**Files:**
- Modify: `app/api/v1/sources.py` (`retry_source`), `app/schemas/source.py` (new `RetryRequest`)
- Test: `tests/test_ingestion_recovery.py`

**Interfaces:**
- Produces: `POST /api/v1/sources/{source_id}/retry` accepts optional body `RetryRequest {confirm: bool = False}`; 409 details `{"code": "confirm_required", "message": …}` and `{"code": "ingesting", "message": …}`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_ingestion_recovery.py`, change `test_retry_endpoint_requeues_a_finished_source` to post `json={"confirm": True}`, change `test_retry_refuses_while_a_job_holds_the_claim` to also assert `response.json()["detail"]["code"] == "ingesting"`, and add:

```python
async def test_reprocessing_a_finished_source_needs_confirmation(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The source already exists in the library; replacing its passages is the learner's call."""
    store = InMemoryBlobStore()
    source = await _source(db_session, store, owner=api_learner)
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    for body in ({}, {"confirm": False}, None):
        response = await api_client.post(
            f"/api/v1/sources/{source.id}/retry", **({} if body is None else {"json": body})
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "confirm_required"

    db_session.expire_all()
    assert (await db_session.get(Source, source.id)).status == SourceStatus.DONE


async def test_a_failed_source_retries_without_asking(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    store = InMemoryBlobStore()
    source = await _source(db_session, store, owner=api_learner)
    source.status = SourceStatus.FAILED
    await db_session.commit()

    response = await api_client.post(f"/api/v1/sources/{source.id}/retry")

    assert response.status_code == 202
    assert response.json()["status"] == SourceStatus.PENDING
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ingestion_recovery.py -k "retry or reprocessing" -v`
Expected: FAIL — the unconfirmed finished source answers 202.

- [ ] **Step 3: Implement.** `app/schemas/source.py`:

```python
class RetryRequest(BaseModel):
    """Re-processing a finished source replaces its passages, so it has to be confirmed (S29)."""

    confirm: bool = False
```

`app/api/v1/sources.py` (`from fastapi import Body`; import `RetryRequest`, `SourceStatus`):

```python
_CONFIRM_REQUIRED = (
    "This source is already in your library. Re-processing it replaces its passages; older "
    "replies will show their citations as an earlier version."
)


@router.post(
    "/sources/{source_id}/retry",
    response_model=SourceRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_source(
    source_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    enqueue: IngestionEnqueuerDep,
    data: Annotated[RetryRequest | None, Body()] = None,
):
    """Re-run ingestion for a finished or failed source.

    A completed source is deliberately not claimable by a job (S37), so re-ingesting one has
    to be asked for — and, since it replaces passages the learner's replies may cite, confirmed
    (S29). A failed source has nothing to replace and retries directly. 409 while a claim is
    live rather than yanking work in flight; the two 409s carry different codes.
    """
    source = await session.get(Source, source_id)
    if source is None or source.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    if source.status == SourceStatus.DONE and not (data is not None and data.confirm):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "confirm_required", "message": _CONFIRM_REQUIRED},
        )
    try:
        reset = await svc.reset_for_reingest(session, source_id)
    except svc.WebIngestionDisabled as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if reset is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "ingesting", "message": "This source is being ingested right now."},
        )
    await svc.dispatch(enqueue, reset.id)
    return reset
```

(Keep the existing decorator; replace only the function if the decorator already matches.)

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_ingestion_recovery.py tests/test_v0_web_policy.py -v` → PASS. Check the frontend does not already parse this 409's `detail` as a string: `grep -rn "retry" frontend/src/api` (expected: nothing).
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/sources.py app/schemas/source.py frontend/src/api/schema.d.ts tests/test_ingestion_recovery.py
git status
git commit -m "feat(sources): re-processing a finished source asks first [S29]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The reindex service and `poe reindex` [S50]

**Files:**
- Create: `app/services/reindex.py`, `app/workers/reindex.py`
- Modify: `pyproject.toml` (poe task), `docs/RUNBOOK.md` (new §15)
- Test: `tests/test_reindex.py` (create)

**Interfaces:**
- Consumes: `PIPELINE_VERSION`, `embed_in_batches`, `PartialEmbedding` (`app.rag.pipeline`); `current_space` (`app.llm.embedding_space`); `ingestion.reset_for_reingest`, `ingestion.dispatch`; `log_llm_call`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class StaleSource: source_id: uuid.UUID; learner_id: uuid.UUID; origin: str; chunks: int
  @dataclass(frozen=True)
  class ScopeRepair: source_id: uuid.UUID; origin: str; before: tuple[uuid.UUID | None, uuid.UUID | None]; after: tuple[uuid.UUID | None, uuid.UUID | None]
  @dataclass
  class ReindexPlan: reembed: list[StaleSource]; reextract: list[StaleSource]; scope: list[ScopeRepair]
  @dataclass
  class ReindexResult: reembedded: list[uuid.UUID]; reextracted: list[uuid.UUID]; busy: list[uuid.UUID]; failed: dict[uuid.UUID, str]; scope_repaired: int
  async def plan(session, *, space: str, learner_id: uuid.UUID | None = None) -> ReindexPlan
  async def apply(session, llm, found: ReindexPlan, *, space: str, reextract: bool, limit: int | None, enqueue: Callable[[uuid.UUID], Awaitable[None]], settings: Settings) -> ReindexResult
  def render(found: ReindexPlan, result: ReindexResult | None) -> str
  ```

- [ ] **Step 1: Write the failing tests** — `tests/test_reindex.py`:

```python
"""The operator reindex (S50): staleness derived from the data, so a second run is a resume."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.embedding_space import current_space
from app.llm.registry import fake_llm_client
from app.models.chat import LLMCall
from app.models.knowledge import Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import pipeline
from app.services import ingestion, reindex
from app.storage import InMemoryBlobStore

OLD_SPACE = "ollama:old-embedder:768"


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _done_source(
    session: AsyncSession, learner: Learner, text: bytes = b"Mitochondria make ATP."
) -> Source:
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex[:6]}.txt",
        content_type="text/plain",
        data=text,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return source


async def _chunks(session: AsyncSession, source: Source) -> list[Chunk]:
    session.expire_all()
    return list((await session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all())


def _space() -> str:
    return current_space(fake_llm_client(), dim=get_settings().embed_dim)


async def _age(session: AsyncSession, source: Source, *, space: str | None = None,
               version: int | None = None) -> None:
    for chunk in await _chunks(session, source):
        if space is not None:
            chunk.embedding_space = space
        if version is not None:
            chunk.pipeline_version = version
    await session.commit()


async def test_a_dry_run_lists_and_changes_nothing(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    await _age(db_session, source, space=OLD_SPACE)

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)

    assert [s.source_id for s in found.reembed] == [source.id]
    assert {c.embedding_space for c in await _chunks(db_session, source)} == {OLD_SPACE}
    assert "re-embed" in reindex.render(found, None)


async def test_apply_re_embeds_in_place_and_a_second_run_finds_nothing(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    before = {c.id for c in await _chunks(db_session, source)}
    await _age(db_session, source, space=OLD_SPACE)
    calls_before = await db_session.scalar(select(func.count()).select_from(LLMCall))

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=False, limit=None,
        enqueue=_Queue(), settings=get_settings(),
    )

    after = await _chunks(db_session, source)
    assert result.reembedded == [source.id]
    assert {c.id for c in after} == before, "same chunks, so every citation still resolves"
    assert {c.embedding_space for c in after} == {_space()}
    assert await db_session.scalar(select(func.count()).select_from(LLMCall)) > calls_before
    again = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    assert again.reembed == [] and again.reextract == []


async def test_one_failure_does_not_stop_the_run(
    db_session: AsyncSession, monkeypatch
) -> None:
    learner = await _learner(db_session)
    bad = await _done_source(db_session, learner, b"boom boom boom")
    good = await _done_source(db_session, learner, b"Chloroplasts hold chlorophyll.")
    await _age(db_session, bad, space=OLD_SPACE)
    await _age(db_session, good, space=OLD_SPACE)
    llm = fake_llm_client()
    original = llm.embed

    async def flaky(role, texts, **kwargs):
        if any("boom" in t for t in texts):
            raise RuntimeError("provider down")
        return await original(role, texts, **kwargs)

    monkeypatch.setattr(llm, "embed", flaky)

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session, llm, found, space=_space(), reextract=False, limit=None,
        enqueue=_Queue(), settings=get_settings(),
    )

    assert result.reembedded == [good.id]
    assert set(result.failed) == {bad.id}


async def test_limit_caps_the_sources_touched(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    for text in (b"one fact", b"two facts", b"three facts"):
        await _age(db_session, await _done_source(db_session, learner, text), space=OLD_SPACE)

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    result = await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=False, limit=2,
        enqueue=_Queue(), settings=get_settings(),
    )

    assert len(result.reembedded) == 2


async def test_learner_restricts_the_plan(db_session: AsyncSession) -> None:
    mine, theirs = await _learner(db_session), await _learner(db_session)
    await _age(db_session, await _done_source(db_session, mine), space=OLD_SPACE)
    await _age(db_session, await _done_source(db_session, theirs), space=OLD_SPACE)

    found = await reindex.plan(db_session, space=_space(), learner_id=mine.id)

    assert {s.learner_id for s in found.reembed} == {mine.id}


async def test_reextract_resets_and_enqueues_only_with_the_flag(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    await _age(db_session, source, version=pipeline.PIPELINE_VERSION - 1)
    queue = _Queue()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    without = await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=False, limit=None,
        enqueue=queue, settings=get_settings(),
    )
    assert [s.source_id for s in found.reextract] == [source.id]
    assert without.reextracted == [] and queue.enqueued == []

    with_flag = await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=True, limit=None,
        enqueue=queue, settings=get_settings(),
    )
    assert with_flag.reextracted == [source.id]
    assert queue.enqueued == [source.id]
    db_session.expire_all()
    assert (await db_session.get(Source, source.id)).status == SourceStatus.PENDING


async def test_reextract_skips_a_source_mid_ingest(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    await _age(db_session, source, version=pipeline.PIPELINE_VERSION - 1)
    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    await ingestion.reset_for_reingest(db_session, source.id)
    await ingestion.claim_source(db_session, source.id, settings=get_settings())

    result = await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=True, limit=None,
        enqueue=_Queue(), settings=get_settings(),
    )

    assert result.busy == [source.id]


async def test_a_source_with_only_history_left_is_not_stale(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _done_source(db_session, learner)
    for chunk in await _chunks(db_session, source):
        chunk.embedding_space = OLD_SPACE
        chunk.superseded_at = func.now()
        chunk.embedding = None
    await db_session.commit()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)

    assert found.reembed == [] and found.reextract == []


async def test_scope_repair_takes_the_topics_subject_when_there_is_none(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio", owner_learner_id=learner.id)
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="Cells")
    db_session.add(topic)
    await db_session.flush()
    source = await _done_source(db_session, learner)
    source.topic_id = topic.id
    source.subject_id = None
    await db_session.commit()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=False, limit=None,
        enqueue=_Queue(), settings=get_settings(),
    )

    db_session.expire_all()
    repaired = await db_session.get(Source, source.id)
    assert (repaired.subject_id, repaired.topic_id) == (subject.id, topic.id)


async def test_scope_repair_clears_a_topic_from_another_subject(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    s1 = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio", owner_learner_id=learner.id)
    s2 = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chem", owner_learner_id=learner.id)
    db_session.add_all([s1, s2])
    await db_session.flush()
    topic = Topic(subject_id=s2.id, slug="t", name="Bonds")
    db_session.add(topic)
    await db_session.flush()
    source = await _done_source(db_session, learner)
    source.subject_id, source.topic_id = s1.id, topic.id
    await db_session.commit()

    found = await reindex.plan(db_session, space=_space(), learner_id=learner.id)
    assert [r.source_id for r in found.scope] == [source.id]
    await reindex.apply(
        db_session, fake_llm_client(), found, space=_space(), reextract=False, limit=None,
        enqueue=_Queue(), settings=get_settings(),
    )

    db_session.expire_all()
    repaired = await db_session.get(Source, source.id)
    assert (repaired.subject_id, repaired.topic_id) == (s1.id, None)
```

Change the test module's `from sqlalchemy import select` to `from sqlalchemy import func, select`. If `claim_source` refuses to claim right after `reset_for_reingest` in the busy test, set `source.status = SourceStatus.PROCESSING` and a future `lease_expires_at` directly instead.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_reindex.py -v`
Expected: FAIL — `ImportError: cannot import name 'reindex' from 'app.services'`.

- [ ] **Step 3: Implement `app/services/reindex.py`:**

```python
"""Bring stale sources up to date (S50), deciding what is stale from the data every time.

Two things can make a source's chunks stale. A different embedding model makes their vectors
incomparable to new queries; the text is still right, so the chunks are re-embedded where they
are and every citation keeps resolving. A newer extraction/chunking pipeline may make the text
itself come out differently; only a re-ingest fixes that, which gives the chunks new ids, so it
happens only when the operator asks for it (``--reextract``). Scope repair brings legacy
subject/topic tags into line with the rule S55 enforces on new ones.

Nothing records progress: what is stale is read from the chunks, so a run that stops halfway is
resumed by running it again.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.llm import LLMClient, ModelRole
from app.models.knowledge import Subject, Topic
from app.models.source import Chunk, Source, SourceStatus
from app.rag.pipeline import PIPELINE_VERSION, PartialEmbedding, embed_in_batches
from app.services import ingestion
from app.services.llm_log import log_llm_call

log = structlog.get_logger()


@dataclass(frozen=True)
class StaleSource:
    source_id: uuid.UUID
    learner_id: uuid.UUID
    origin: str
    chunks: int


@dataclass(frozen=True)
class ScopeRepair:
    source_id: uuid.UUID
    origin: str
    before: tuple[uuid.UUID | None, uuid.UUID | None]  # (subject_id, topic_id)
    after: tuple[uuid.UUID | None, uuid.UUID | None]


@dataclass
class ReindexPlan:
    reembed: list[StaleSource] = field(default_factory=list)
    reextract: list[StaleSource] = field(default_factory=list)
    scope: list[ScopeRepair] = field(default_factory=list)


@dataclass
class ReindexResult:
    reembedded: list[uuid.UUID] = field(default_factory=list)
    reextracted: list[uuid.UUID] = field(default_factory=list)
    busy: list[uuid.UUID] = field(default_factory=list)
    failed: dict[uuid.UUID, str] = field(default_factory=dict)
    scope_repaired: int = 0


async def _stale(
    session: AsyncSession, condition, learner_id: uuid.UUID | None
) -> list[StaleSource]:
    stmt = (
        select(Source.id, Source.learner_id, Source.origin, func.count(Chunk.id))
        .join(Chunk, Chunk.source_id == Source.id)
        .where(Source.status == SourceStatus.DONE, Chunk.superseded_at.is_(None))
        .group_by(Source.id)
        .having(func.bool_or(condition))
        .order_by(Source.created_at)
    )
    if learner_id is not None:
        stmt = stmt.where(Source.learner_id == learner_id)
    return [StaleSource(*row) for row in (await session.execute(stmt)).all()]


def repair(
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    subject_owner: uuid.UUID | None,
    subject_exists: bool,
    topic_id: uuid.UUID | None,
    topic_subject_id: uuid.UUID | None,
    topic_owner: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """The scope a source should have — S55's rule, applied to a row that predates it."""
    visible_subject = subject_exists and subject_owner in (None, learner_id)
    if subject_id is not None and not visible_subject:
        subject_id = None
    if topic_id is None:
        return subject_id, None
    if topic_owner not in (None, learner_id):
        return subject_id, None
    if subject_id is None:
        return topic_subject_id, topic_id
    return (subject_id, topic_id) if topic_subject_id == subject_id else (subject_id, None)


async def _scope_repairs(session: AsyncSession, learner_id: uuid.UUID | None) -> list[ScopeRepair]:
    own = aliased(Subject)
    topic_subject = aliased(Subject)
    stmt = (
        select(
            Source.id, Source.origin, Source.learner_id, Source.subject_id, own.id,
            own.owner_learner_id, Source.topic_id, Topic.subject_id,
            topic_subject.owner_learner_id,
        )
        .outerjoin(own, own.id == Source.subject_id)
        .outerjoin(Topic, Topic.id == Source.topic_id)
        .outerjoin(topic_subject, topic_subject.id == Topic.subject_id)
        .where((Source.subject_id.is_not(None)) | (Source.topic_id.is_not(None)))
    )
    if learner_id is not None:
        stmt = stmt.where(Source.learner_id == learner_id)
    repairs = []
    for sid, origin, owner, subj, subj_row, subj_owner, topic, t_subj, t_owner in (
        await session.execute(stmt)
    ).all():
        after = repair(
            learner_id=owner,
            subject_id=subj,
            subject_owner=subj_owner,
            subject_exists=subj_row is not None,
            topic_id=topic,
            topic_subject_id=t_subj,
            topic_owner=t_owner,
        )
        if after != (subj, topic):
            repairs.append(ScopeRepair(sid, origin, (subj, topic), after))
    return repairs


async def plan(
    session: AsyncSession, *, space: str, learner_id: uuid.UUID | None = None
) -> ReindexPlan:
    """What is stale right now. Changes nothing."""
    return ReindexPlan(
        reembed=await _stale(session, Chunk.embedding_space != space, learner_id),
        reextract=await _stale(session, Chunk.pipeline_version < PIPELINE_VERSION, learner_id),
        scope=await _scope_repairs(session, learner_id),
    )


async def _reembed(
    session: AsyncSession, llm: LLMClient, stale: StaleSource, *, space: str, settings: Settings
) -> None:
    chunks = list(
        (
            await session.scalars(
                select(Chunk)
                .where(Chunk.source_id == stale.source_id, Chunk.superseded_at.is_(None))
                .order_by(Chunk.ordinal)
            )
        ).all()
    )
    try:
        embedded = await embed_in_batches(
            llm,
            [c.text for c in chunks],
            batch_size=settings.embed_batch_size,
            concurrency=settings.embed_concurrency,
        )
    except PartialEmbedding as exc:
        if exc.usage.total_tokens:
            await log_llm_call(
                learner_id=stale.learner_id,
                role=str(ModelRole.EMBED),
                spec=llm.spec(ModelRole.EMBED),
                usage=exc.usage,
            )
        raise
    for chunk, vector in zip(chunks, embedded.vectors, strict=True):
        chunk.embedding = vector
        chunk.embedding_space = space
    if embedded.usage.total_tokens:
        await log_llm_call(
            learner_id=stale.learner_id,
            role=str(ModelRole.EMBED),
            spec=llm.spec(ModelRole.EMBED),
            usage=embedded.usage,
        )
    await session.commit()


async def apply(
    session: AsyncSession,
    llm: LLMClient,
    found: ReindexPlan,
    *,
    space: str,
    reextract: bool,
    limit: int | None,
    enqueue: Callable[[uuid.UUID], Awaitable[None]],
    settings: Settings,
) -> ReindexResult:
    """Carry out ``found``. One source's failure is recorded and the run moves on."""
    result = ReindexResult()
    for fix in found.scope:
        source = await session.get(Source, fix.source_id)
        if source is not None:
            source.subject_id, source.topic_id = fix.after
            result.scope_repaired += 1
    await session.commit()

    budget = limit if limit is not None else len(found.reembed) + len(found.reextract)
    reextracting = {s.source_id for s in found.reextract} if reextract else set()
    if reextract:
        for stale in found.reextract:
            if budget <= 0:
                break
            budget -= 1
            reset = await ingestion.reset_for_reingest(session, stale.source_id)
            if reset is None:
                result.busy.append(stale.source_id)
                continue
            await ingestion.dispatch(enqueue, stale.source_id)
            result.reextracted.append(stale.source_id)
    for stale in found.reembed:
        if stale.source_id in reextracting:
            continue
        if budget <= 0:
            break
        budget -= 1
        try:
            await _reembed(session, llm, stale, space=space, settings=settings)
        except Exception as exc:  # reported, and the next run retries it
            await session.rollback()
            log.warning("reindex.reembed_failed", source_id=str(stale.source_id), error=str(exc))
            result.failed[stale.source_id] = str(exc)
            continue
        result.reembedded.append(stale.source_id)
    return result


def render(found: ReindexPlan, result: ReindexResult | None) -> str:
    """The operator's report: what is stale, and what this run did about it."""
    lines = [
        f"re-embed: {len(found.reembed)} sources, "
        f"{sum(s.chunks for s in found.reembed)} chunks (embedding model changed)",
        *(f"  {s.source_id}  {s.origin}  {s.chunks} chunks" for s in found.reembed),
        f"re-extract: {len(found.reextract)} sources (pipeline version below {PIPELINE_VERSION})",
        *(f"  {s.source_id}  {s.origin}  {s.chunks} chunks" for s in found.reextract),
        f"scope repair: {len(found.scope)} sources",
        *(f"  {r.source_id}  {r.origin}  {r.before} -> {r.after}" for r in found.scope),
    ]
    if result is None:
        lines.append("dry run: nothing changed. --apply to re-embed and repair scope.")
    else:
        lines += [
            f"re-embedded {len(result.reembedded)}, re-extract queued {len(result.reextracted)}, "
            f"scope repaired {result.scope_repaired}",
            *(f"  busy (skipped): {sid}" for sid in result.busy),
            *(f"  failed: {sid}  {err}" for sid, err in result.failed.items()),
        ]
    return "\n".join(lines)
```

- [ ] **Step 4: CLI** `app/workers/reindex.py`:

```python
"""Bring stale sources up to date — ``uv run poe reindex [--apply] [--reextract]
[--learner ID] [--limit N]``. Dry run by default. See docs/RUNBOOK.md §15."""

import argparse
import asyncio
import uuid

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.llm.embedding_space import current_space
from app.llm.registry import build_llm_client
from app.services import reindex
from app.workers.tasks import _enqueue_ingestion


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    llm = build_llm_client(settings)
    space = current_space(llm, dim=settings.embed_dim)
    async with SessionFactory() as session:
        found = await reindex.plan(session, space=space, learner_id=args.learner)
        result = None
        if args.apply:
            result = await reindex.apply(
                session,
                llm,
                found,
                space=space,
                reextract=args.reextract,
                limit=args.limit,
                enqueue=_enqueue_ingestion,
                settings=settings,
            )
        print(reindex.render(found, result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="re-embed and repair scope")
    parser.add_argument(
        "--reextract", action="store_true", help="with --apply: also re-ingest stale extractions"
    )
    parser.add_argument("--learner", type=uuid.UUID, help="only this learner's sources")
    parser.add_argument("--limit", type=int, help="at most N sources re-embedded/re-extracted")
    args = parser.parse_args()
    if args.reextract and not args.apply:
        parser.error("--reextract needs --apply")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

Check the real name of the function that builds the configured `LLMClient` (`grep -n "^def " app/llm/registry.py`) and of the worker's enqueue callable (`grep -n "_enqueue_ingestion" app/workers/*.py app/api/deps.py`); use those. `parser.error` exits with code 2, as the spec requires.

`pyproject.toml`, beside `decision-report`: `reindex = "python -m app.workers.reindex"`.

- [ ] **Step 5: RUNBOOK §15.** Append to `docs/RUNBOOK.md`:

```markdown
## 15. Reindexing sources (S29, S50)

A chunk records the embedding space and the pipeline version that wrote it. `uv run poe reindex`
compares both against the running configuration and lists what is stale; it changes nothing
without `--apply`.

1. `uv run poe reindex` — dry run. Read the three groups: re-embed (the embedding model changed),
   re-extract (`PIPELINE_VERSION` in `app/rag/pipeline.py` was bumped), scope repair (legacy
   subject/topic tags that disagree with the graph).
2. `uv run poe reindex --apply [--limit N]` — re-embeds in place and repairs scope. Chunk ids do
   not change, so every citation keeps resolving. Costs one embed per chunk; the dry run's chunk
   count is the bill. Interrupting is safe: run it again and it continues.
3. `uv run poe reindex --apply --reextract [--limit N]` — also re-ingests sources whose
   extraction is stale, through the normal ingestion queue. This gives their chunks new ids;
   chunks something cites are kept as "earlier version" history (S29), the rest are deleted.

Bump `PIPELINE_VERSION` whenever a change to extraction or chunking changes chunk text. Do not
bump it for changes that only affect tagging or metadata.
```

- [ ] **Step 6: Run tests and the gate**

Run: `uv run pytest tests/test_reindex.py -v` → PASS.
Run: `uv run poe reindex --help` → prints usage (exit 0). Do not run it with `--apply` against the dev database.
Run: `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 7: Commit**

```bash
git add app/services/reindex.py app/workers/reindex.py pyproject.toml docs/RUNBOOK.md tests/test_reindex.py
git status
git commit -m "feat(ops): resumable reindex for stale embeddings, extractions and scope [S50]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Retry / Re-process in the source list, and honest citation states [S29]

**Files:**
- Create: `frontend/src/components/uploads/SourceActions.tsx`, `frontend/src/components/uploads/SourceActions.test.tsx`, `frontend/src/components/chat/CitationPane.test.tsx`
- Modify: `frontend/src/components/uploads/SourceList.tsx`, `frontend/src/api/hooks.ts`, `frontend/src/components/chat/CitationPane.tsx`

**Interfaces:**
- Consumes: `POST /api/v1/sources/{source_id}/retry` with `{confirm}` (Task 3); `ChunkRead.superseded` (Task 1).
- Produces: `SourceActions({ status, onRetry, pending })` where `onRetry(confirm: boolean)`; `CitationBody({ origin, locator, text, superseded, missing })`; `useRetrySource()`.

- [ ] **Step 1: Write the failing tests.** `SourceActions.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceActions } from "./SourceActions";

describe("SourceActions", () => {
  it("retries a failed source straight away", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="failed" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledWith(false);
  });

  it("asks before re-processing a finished source", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="done" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Re-process" }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.getByText(/already in your library/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Replace passages" }));
    expect(onRetry).toHaveBeenCalledWith(true);
  });

  it("lets the learner back out", async () => {
    const onRetry = vi.fn();
    render(<SourceActions status="done" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Re-process" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onRetry).not.toHaveBeenCalled();
    expect(screen.queryByText(/already in your library/)).not.toBeInTheDocument();
  });

  it("offers nothing while a source is being processed", () => {
    const { container } = render(<SourceActions status="processing" onRetry={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
```

`CitationPane.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CitationBody } from "./CitationPane";

describe("CitationBody", () => {
  it("labels a passage from an earlier version of its source", () => {
    render(<CitationBody origin="notes.pdf" locator="Page 3" text="old words" superseded />);
    expect(screen.getByText("From an earlier version of this source")).toBeInTheDocument();
    expect(screen.getByText("old words")).toBeInTheDocument();
  });

  it("says plainly when the passage is gone", () => {
    render(<CitationBody missing />);
    expect(screen.getByText("This passage is no longer available.")).toBeInTheDocument();
    expect(screen.queryByText("Unknown source")).not.toBeInTheDocument();
  });

  it("shows a current passage without a label", () => {
    render(<CitationBody origin="notes.pdf" text="current words" />);
    expect(screen.getByText("current words")).toBeInTheDocument();
    expect(screen.queryByText(/earlier version/)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/uploads/SourceActions.test.tsx src/components/chat/CitationPane.test.tsx`
Expected: FAIL — cannot resolve `./SourceActions`; `CitationBody` is not exported.

- [ ] **Step 3: Implement `SourceActions.tsx`:**

```tsx
import { useState } from "react";

const CONFIRM =
  "This source is already in your library. Re-processing it replaces its passages; older " +
  "replies will show their citations as an earlier version.";

/** Retry for a failed source; Re-process, confirmed first, for a finished one (S29). A
 * finished source's passages may be cited by replies the learner already has, so replacing
 * them is their decision, made with the consequence in front of them. */
export function SourceActions({
  status,
  onRetry,
  pending,
}: {
  status: string;
  onRetry: (confirm: boolean) => void;
  pending?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  if (status === "failed") {
    return (
      <button className="btn btn-ghost btn-xs" disabled={pending} onClick={() => onRetry(false)}>
        Retry
      </button>
    );
  }
  if (status !== "done") return null;
  if (!asking) {
    return (
      <button className="btn btn-ghost btn-xs" disabled={pending} onClick={() => setAsking(true)}>
        Re-process
      </button>
    );
  }
  return (
    <div className="flex flex-col items-end gap-1">
      <p className="text-caption text-base-content/70 max-w-xs text-right">{CONFIRM}</p>
      <div className="flex gap-1">
        <button className="btn btn-ghost btn-xs" onClick={() => setAsking(false)}>
          Cancel
        </button>
        <button
          className="btn btn-warning btn-xs"
          disabled={pending}
          onClick={() => {
            setAsking(false);
            onRetry(true);
          }}
        >
          Replace passages
        </button>
      </div>
    </div>
  );
}
```

`hooks.ts`, after `useSource`:

```ts
/** Retry a failed source, or re-process a finished one once the learner confirmed (S29). */
export function useRetrySource() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ sourceId, confirm }: { sourceId: string; confirm: boolean }) => {
      const { data, error } = await api.POST("/api/v1/sources/{source_id}/retry", {
        params: { path: { source_id: sourceId } },
        body: { confirm },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sources"] });
    },
  });
}
```

`SourceList.tsx` `SourceRow`: wrap the badge and actions in `<div className="flex shrink-0 items-center gap-2">`, and render before the badge:

```tsx
<SourceActions
  status={source.status}
  pending={retry.isPending}
  onRetry={(confirm) => retry.mutate({ sourceId: source.id, confirm })}
/>
```

with `const retry = useRetrySource();` at the top of `SourceRow`.

`CitationPane.tsx`: export a presentational body and use it:

```tsx
/** What a citation shows (S29): the passage, labelled when a re-ingest replaced it, or a plain
 * statement when the passage no longer exists at all. */
export function CitationBody({
  origin,
  locator,
  text,
  superseded = false,
  missing = false,
}: {
  origin?: string;
  locator?: string | null;
  text?: string;
  superseded?: boolean;
  missing?: boolean;
}) {
  if (missing) {
    return <p className="text-body text-base-content/60">This passage is no longer available.</p>;
  }
  return (
    <>
      <div>
        <p className="text-caption text-base-content/60 truncate">{origin}</p>
        {locator && <p className="text-caption text-primary">{locator}</p>}
        {superseded && (
          <p className="text-caption text-warning">From an earlier version of this source</p>
        )}
      </div>
      <p className="text-body text-base-content/90 whitespace-pre-wrap">{text}</p>
    </>
  );
}
```

In `CitationPane`, read `isError` from `useChunk` and replace the loaded branch with
`<CitationBody missing={chunkError || !chunk} origin={source?.origin ?? "Your source"} locator={locator} text={chunk?.text} superseded={chunk?.superseded ?? false} />`.

- [ ] **Step 4: Run tests and the build**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build` → green. Run `npx prettier --check` and `npx eslint` on the changed files.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/uploads/SourceActions.tsx frontend/src/components/uploads/SourceActions.test.tsx frontend/src/components/uploads/SourceList.tsx frontend/src/api/hooks.ts frontend/src/components/chat/CitationPane.tsx frontend/src/components/chat/CitationPane.test.tsx
git status
git commit -m "feat(web): retry and confirmed re-process, and citations that say what happened [S29]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Tracker [S50]

**Files:** Modify `docs/guru-suggestions-tracker.md` (rows S29, S50), `CLAUDE.md`.

- [ ] **Step 1:** S29 → **Completed**: re-ingest supersedes instead of deleting; cited chunks kept (text and locator, no vector or tags), uncited deleted; re-processing a finished source needs the learner's confirmation; citation pane labels earlier versions and missing passages. Evidence: `app/rag/pipeline.py`, `tests/test_supersede.py`, `tests/test_ingestion_recovery.py`.
- [ ] **Step 2:** S50 → **Partial**: chunks record `pipeline_version`; `uv run poe reindex` (dry run → `--apply` re-embeds in place → `--reextract`) derives staleness from the data and is resumable; legacy scope repaired. Remaining: run it against a real corpus; bump `PIPELINE_VERSION` when S27's extraction work lands. Evidence: `app/services/reindex.py`, `tests/test_reindex.py`, RUNBOOK §15.
- [ ] **Step 3:** `CLAUDE.md` Key Technical Decisions, after the S26/S28 bullet:

```markdown
- **A citation outlives a re-ingest** (S29/S50) — re-ingesting supersedes chunks rather than
  deleting them when something cites them, and re-processing a finished source is the
  learner's confirmed decision. `uv run poe reindex` re-embeds in place (ids kept) and only
  re-extracts when asked; staleness is read from the chunks, so a run resumes by running again.
  See [docs/RUNBOOK.md](docs/RUNBOOK.md) §15.
```

- [ ] **Step 4:** `uv run poe check` → green, then:

```bash
git add docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record versioned sources and reindex in the tracker [S50]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
