# Archive, Delete and Forget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A learner can archive (reversibly, out of use), delete (immediately, after seeing what stays) and optionally forget what was derived from a source or conversation.

**Architecture:** Migration 0066 adds `archived_at` to sources and conversations and a FK-free `memories.origin_conversation_id`. `app/services/removal.py` owns archive/unarchive, the impact report, deletion and forgetting, with one provenance resolver per kind. Retrieval — the single place a scope is applied — drops archived sources, so every generation path respects archive. Routes are thin; the frontend gets one `RemovalDialog`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, pytest; React + TypeScript, TanStack Query, vitest.

**Spec:** `docs/superpowers/specs/2026-09-26-archive-delete-forget-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract` (after `uv run poe api-types`, stage `frontend/src/api/schema.d.ts` — the contract check compares against the index).
- Frontend changes also green on `cd frontend && npm run build`, `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npx prettier --check` and `npx eslint` on changed files (no new warnings; a component file exports only components).
- New migration → `uv run python -m tests.testdb`.
- In async tests never call `session.expire_all()`; re-read with `session.get(..., populate_existing=True)` / `execution_options(populate_existing=True)`, and read ids into locals before code that may roll back.
- One tracker id per commit subject: `[S61]` or `[S42]` as each task states.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls; tests use `fake_llm_client`.
- Not-yours and not-found are the same 404. Refusals are 409 with `{"code": ..., "message": ...}`.

## Review Focus

1. Deleting a source that is the *original* of a healthy duplicate — the duplicate's FK nulls, the sweep releases it, and it re-ingests from its own file (Task 4 test).
2. Archiving a source that a conversation explicitly picked (`conversation_sources`) — the conversation still exists and simply retrieves nothing from it (Task 2 test: picked scope returns no hits).
3. Deleting a conversation twice / forgetting an origin twice — second call is 404 / `forgotten: 0`, never an error (Task 4/5 tests).
4. A memory the learner corrected (origin None) is never forgotten by an origin sweep (Task 5 test).
5. Unarchiving a source whose same-text twin was deleted meanwhile — no twin, so it simply answers again with its own chunks (Task 2 test).

---

### Task 1: Migration 0066, columns and memory origin [S61]

**Files:**
- Create: `db/migrations/versions/0066_archive_and_memory_origin.py`
- Modify: `app/models/source.py` (`Source`), `app/models/chat.py` (`Conversation`), `app/models/memory.py` (`Memory`), `app/services/memory.py` (`write_back` creation site), `app/schemas/source.py` (`SourceRead`), `app/schemas/chat.py` (`ConversationRead`)
- Test: `tests/test_migrations_with_data.py`, `tests/test_removal.py` (create)

**Interfaces:**
- Produces: `Source.archived_at`, `Conversation.archived_at: datetime | None`; `Memory.origin_conversation_id: uuid.UUID | None`; `SourceRead.archived_at`, `ConversationRead.archived_at: datetime | None = None`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_migrations_with_data.py`:

```python
async def test_memories_keep_where_they_came_from_across_the_archive_migration() -> None:
    """0066 (S61): the origin is copied from the FK, which a conversation delete nulls; the
    copy has no FK, so it survives the delete and "forget" can still find the memory."""
    async with database_at("0065_source_duplicate_of") as connect:
        conn = await connect()
        try:
            learner_id, conversation_id, memory_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            dim = get_settings().embed_dim
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "rememberer"
            )
            await conn.execute(
                "INSERT INTO conversations (id, learner_id) VALUES ($1, $2)",
                conversation_id,
                learner_id,
            )
            await conn.execute(
                "INSERT INTO memories (id, learner_id, conversation_id, kind, content, embedding, "
                "embedding_space, status) VALUES ($1, $2, $3, 'fact', 'likes mornings', "
                "$4::vector, 'fake:fake-1:x', 'current')",
                memory_id,
                learner_id,
                conversation_id,
                "[" + ",".join(["0.1"] * dim) + "]",
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0066_archive_and_memory_origin")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT origin_conversation_id FROM memories WHERE id = $1", memory_id
            )
            assert row is not None and row["origin_conversation_id"] == conversation_id
            await conn.execute("DELETE FROM conversations WHERE id = $1", conversation_id)
            row = await conn.fetchrow(
                "SELECT conversation_id, origin_conversation_id FROM memories WHERE id = $1",
                memory_id,
            )
            assert row is not None
            assert row["conversation_id"] is None
            assert row["origin_conversation_id"] == conversation_id
        finally:
            await conn.close()
```

If the `conversations` insert needs more NOT NULL columns (e.g. `kind`, `phase`), read `app/models/chat.py` and supply their defaults in the INSERT.

Create `tests/test_removal.py`:

```python
"""Archive, delete and forget (S61, S42; V11)."""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.memory import Memory, MemoryKind, MemoryStatus
from app.services import memory as memory_svc
from tests.embedding import FAKE_SPACE


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _memory(
    session: AsyncSession, learner: Learner, conversation: Conversation | None, content: str
) -> Memory:
    embedding = (await fake_llm_client().embed(ModelRole.EMBED, [content])).vectors[0]
    memory = Memory(
        embedding_space=FAKE_SPACE,
        learner_id=learner.id,
        conversation_id=conversation.id if conversation else None,
        origin_conversation_id=conversation.id if conversation else None,
        kind=MemoryKind.FACT,
        content=content,
        embedding=embedding,
    )
    session.add(memory)
    await session.flush()
    return memory


async def test_write_back_records_where_a_memory_came_from(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(
        Message(conversation_id=conversation.id, role="user", content="I study in the mornings.")
    )
    await db_session.commit()
    reply = json.dumps({"memories": [{"kind": "preference", "content": "Studies in mornings"}]})

    created = await memory_svc.write_back(
        db_session, fake_llm_client(reply=reply), conversation_id=conversation.id
    )

    assert created and all(m.origin_conversation_id == conversation.id for m in created)
```

(Check `app/learning/memory_extraction.py` — or wherever `extract_memories` parses — for the exact JSON shape the fake reply must have; match it.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_removal.py -q`
Expected: FAIL — `TypeError: 'origin_conversation_id' is an invalid keyword argument for Memory` (or AttributeError).

- [ ] **Step 3: Migration** `db/migrations/versions/0066_archive_and_memory_origin.py`:

```python
"""Archive for sources and conversations; where a memory came from (S61, V11).

``archived_at`` puts a source or conversation out of the way and out of use without deleting
anything. ``memories.origin_conversation_id`` copies ``conversation_id`` with no foreign key:
the FK nulls when the conversation is deleted, and the copy is what lets a learner still
forget what was learned there afterwards.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_archive_and_memory_origin"
down_revision: str | Sequence[str] | None = "0065_source_duplicate_of"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("memories", sa.Column("origin_conversation_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_memories_origin_conversation_id", "memories", ["origin_conversation_id"]
    )
    op.execute("UPDATE memories SET origin_conversation_id = conversation_id")


def downgrade() -> None:
    op.drop_index("ix_memories_origin_conversation_id", table_name="memories")
    op.drop_column("memories", "origin_conversation_id")
    op.drop_column("conversations", "archived_at")
    op.drop_column("sources", "archived_at")
```

Run `uv run python -m tests.testdb`.

- [ ] **Step 4: Models, write-back, schemas.**
  - `Source` (after `duplicate_of_id`) and `Conversation` (near `created_at`), with `DateTime` imported from sqlalchemy if missing:

```python
    # Out of the way and out of use, fully reversible (S61, V11): never retrieved while set.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
```

  (For `Conversation` the comment reads: "Out of the way and read-only, fully reversible (S61, V11); its memories stay current.")
  - `Memory`, after `conversation_id`:

```python
    # Where it was learned, with no foreign key: `conversation_id` nulls when the conversation is
    # deleted, and this is what still lets the learner forget what was learned there (S42, V11).
    # None for a memory the learner wrote themselves (a correction).
    origin_conversation_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
```

  - `app/services/memory.py` `write_back`, in the `Memory(...)` constructor: add `origin_conversation_id=conversation_id,`. Leave `correct_memory`'s replacement with no origin (it came from the learner).
  - `SourceRead`: `archived_at: datetime | None = None` after `duplicate_of_id`. `ConversationRead`: `archived_at: datetime | None = None` after `active_item_id`.

- [ ] **Step 5: Run tests and the gate**

Run: `uv run pytest tests/test_removal.py tests/test_migrations_with_data.py tests/test_memory_lifecycle.py -q` → PASS.
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 6: Commit**

```bash
git add db/migrations/versions/0066_archive_and_memory_origin.py app/models/source.py app/models/chat.py app/models/memory.py app/services/memory.py app/schemas/source.py app/schemas/chat.py frontend/src/api/schema.d.ts tests/test_migrations_with_data.py tests/test_removal.py
git status
git commit -m "feat(sources): archive columns and where a memory came from [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Archiving a source [S61]

**Files:**
- Create: `app/services/removal.py`
- Modify: `app/rag/retrieval.py` (`retrieve.scoped`), `app/rag/pipeline.py` (rename `_same_text_source` → `same_text_source`, exclude archived), `app/services/ingestion.py` (`_stranded`: archived original is absent), `app/api/v1/sources.py` (list filter, retry refusal, archive/unarchive routes)
- Test: `tests/test_removal.py`

**Interfaces:**
- Consumes: `Source.archived_at` (Task 1); `pipeline.supersede_chunks(session, source)`.
- Produces:
  ```python
  class RemovalRefused(Exception):
      def __init__(self, code: str, message: str) -> None
  async def set_source_archived(session, learner_id: uuid.UUID, source_id: uuid.UUID, *, archived: bool) -> Source | None
  ```
  Routes: `POST /sources/{source_id}/archive`, `POST /sources/{source_id}/unarchive` → `SourceRead`; `GET /sources?archived=true`.

- [ ] **Step 1: Failing tests** — append to `tests/test_removal.py` (add imports: `from httpx import AsyncClient`; `from app.models.source import Chunk, Source, SourceKind, SourceStatus`; `from app.rag import retrieval`; `from app.rag.scope import SourceScope`; `from app.services import ingestion, removal`; `from app.storage import InMemoryBlobStore`; `from app.core.config import Settings`):

```python
API = "/api/v1"
PHOTO = b"Photosynthesis converts light energy into chemical energy in chloroplasts."
PHOTO_US = b"Photosynthesis converts light energy into chemical energy in chloroplasts!"


async def _source(
    session: AsyncSession, learner: Learner, data: bytes = PHOTO, *, store=None
) -> uuid.UUID:
    store = store or InMemoryBlobStore()
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=f"{uuid.uuid4().hex[:6]}.txt",
        content_type="text/plain",
        data=data,
    )
    await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    return source.id


async def _get(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id, populate_existing=True)
    assert source is not None
    return source


async def _hits(session: AsyncSession, learner: Learner, *picked: uuid.UUID) -> list:
    scope = SourceScope(learner_id=learner.id, source_ids=tuple(picked))
    return await retrieval.retrieve(session, fake_llm_client(), "photosynthesis", scope=scope)


async def _current(session: AsyncSession, source_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(Chunk.id).where(Chunk.source_id == source_id, Chunk.superseded_at.is_(None))
    )
    return list(rows)


async def test_an_archived_source_is_never_retrieved_and_unarchive_restores_it(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    source_id = await _source(db_session, learner)
    before = await _current(db_session, source_id)
    assert await _hits(db_session, learner)

    await removal.set_source_archived(db_session, learner.id, source_id, archived=True)
    assert await _hits(db_session, learner) == []
    assert await _hits(db_session, learner, source_id) == [], "picked, still not read"

    await removal.set_source_archived(db_session, learner.id, source_id, archived=False)
    assert [h.chunk_id for h in await _hits(db_session, learner)] == before


async def test_archiving_someone_elses_source_is_not_found(db_session: AsyncSession) -> None:
    owner, stranger = await _learner(db_session), await _learner(db_session)
    source_id = await _source(db_session, owner)

    assert await removal.set_source_archived(db_session, stranger.id, source_id, archived=True) is None
    assert (await _get(db_session, source_id)).archived_at is None


async def test_an_archived_original_releases_its_duplicate(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    dup = await _source(db_session, learner, PHOTO_US, store=store)
    assert (await _get(db_session, dup)).duplicate_of_id == original

    await removal.set_source_archived(db_session, learner.id, original, archived=True)

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_unarchive_defers_to_a_twin_that_answers_meanwhile(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    dup = await _source(db_session, learner, PHOTO_US, store=store)
    await removal.set_source_archived(db_session, learner.id, original, archived=True)
    await ingestion.reconcile_stranded(db_session, _Queue(), settings=Settings())
    await ingestion.ingest_source(db_session, store, fake_llm_client(), dup)
    assert await _current(db_session, dup), "the duplicate now answers for itself"

    await removal.set_source_archived(db_session, learner.id, original, archived=False)

    source = await _get(db_session, original)
    assert source.duplicate_of_id == dup
    assert await _current(db_session, original) == []


async def test_unarchive_without_a_twin_simply_answers_again(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    await removal.set_source_archived(db_session, learner.id, original, archived=True)

    await removal.set_source_archived(db_session, learner.id, original, archived=False)

    assert (await _get(db_session, original)).duplicate_of_id is None
    assert await _current(db_session, original)


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def __call__(self, source_id: uuid.UUID) -> None:
        self.enqueued.append(source_id)


async def test_the_api_lists_archived_sources_apart_and_refuses_retry(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source_id = await _source(db_session, api_learner)
    await db_session.commit()

    r = await api_client.post(f"{API}/sources/{source_id}/archive")
    assert r.status_code == 200 and r.json()["archived_at"] is not None
    listed = [s["id"] for s in (await api_client.get(f"{API}/sources")).json()]
    archived = [s["id"] for s in (await api_client.get(f"{API}/sources?archived=true")).json()]
    assert str(source_id) not in listed and archived == [str(source_id)]
    r = await api_client.post(f"{API}/sources/{source_id}/retry", json={"confirm": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "archived"

    r = await api_client.post(f"{API}/sources/{source_id}/unarchive")
    assert r.status_code == 200 and r.json()["archived_at"] is None
    r = await api_client.post(f"{API}/sources/{uuid.uuid4()}/archive")
    assert r.status_code == 404
```

(`PHOTO_US` must canonicalise to the same text as `PHOTO` — check `app/rag/textnorm.canonical`; if trailing punctuation is not normalised away, use the BRITISH/AMERICAN pair from `tests/test_duplicate_recovery.py` for the two duplicate tests instead, with a query word they both contain.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_removal.py -q`
Expected: FAIL — `ImportError: cannot import name 'removal'`.

- [ ] **Step 3: Implement.**

`app/rag/retrieval.py`, in `scoped`, add to the first `.where(...)`:

```python
            # Archived sources are out of use until unarchived (S61) — here, the one place a
            # scope is applied, so no generation path can read one.
            Source.archived_at.is_(None),
```

`app/rag/pipeline.py`: rename `_same_text_source` → `same_text_source` (update its one caller in `run`), and add `Source.archived_at.is_(None),` to its `where` (an archived source answers for nothing, so it is no one's twin).

`app/services/ingestion.py` `_stranded`, in the `healthy` subquery's `where`: add `original.archived_at.is_(None),` and extend the docstring sentence "Moved, deleted or emptied" to "Moved, archived, deleted or emptied".

Create `app/services/removal.py`:

```python
"""Archive, delete and forget, for sources and conversations (S61, S42; V11).

Archive is out of the way and out of use, and reversible: nothing is deleted. Delete is final
and says first what it keeps. Forget removes what was *derived* from the thing — lessons built
on a source's passages, memories drawn from a conversation — and never the learner's answers
or mastery, which are evidence of what they can do whatever material it came through.

What derives from what is decided in the provenance resolvers below and nowhere else.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source import Chunk, Source
from app.rag import pipeline


class RemovalRefused(Exception):
    """The request is valid but cannot be carried out now; ``code`` is stable for clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _own_source(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID
) -> Source | None:
    source = await session.get(Source, source_id, populate_existing=True)
    return source if source is not None and source.learner_id == learner_id else None


async def set_source_archived(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID, *, archived: bool
) -> Source | None:
    """Archive or unarchive a source. ``None`` if it is not this learner's.

    Unarchiving re-deduplicates: while it was away a same-text source may have started
    answering in the same scope (its duplicate, released by the sweep), and two answering
    copies crowd each other out of every grounding window. The stored text digest decides, so
    no extraction runs.
    """
    source = await _own_source(session, learner_id, source_id)
    if source is None:
        return None
    source.archived_at = datetime.now(UTC) if archived else None
    if not archived:
        await session.flush()
        twin = await pipeline.same_text_source(session, source)
        if twin is not None:
            source.duplicate_of_id = twin.id
            await pipeline.supersede_chunks(session, source)
    await session.commit()
    await session.refresh(source)
    return source
```

(`Chunk` is used by later tasks; drop the import if ruff flags it now.)

`app/api/v1/sources.py`:
- `list_sources` gains `archived: Annotated[bool, Query()] = False` and `stmt = stmt.where(Source.archived_at.is_not(None) if archived else Source.archived_at.is_(None))`; docstring: "Archived sources are listed only with `archived=true` (S61)."
- `retry_source`: after the URL check, before the confirm check:

```python
    if source.archived_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "archived", "message": "Unarchive this source to process it again."},
        )
```

- New routes (import `from app.services import removal`):

```python
@router.post("/sources/{source_id}/archive", response_model=SourceRead)
async def archive_source(source_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Out of the way and out of use, reversibly (S61): never retrieved while archived."""
    source = await removal.set_source_archived(session, learner.id, source_id, archived=True)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return source


@router.post("/sources/{source_id}/unarchive", response_model=SourceRead)
async def unarchive_source(source_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    source = await removal.set_source_archived(session, learner.id, source_id, archived=False)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return source
```

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_removal.py tests/test_duplicate_recovery.py tests/test_text_dedup.py tests/test_retrieval.py tests/test_sources_api.py -q` → PASS.
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/removal.py app/rag/retrieval.py app/rag/pipeline.py app/services/ingestion.py app/api/v1/sources.py frontend/src/api/schema.d.ts tests/test_removal.py
git status
git commit -m "feat(sources): archive a source out of use, and back [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Archiving a conversation [S61]

**Files:**
- Modify: `app/services/removal.py`, `app/services/chat.py` (`list_conversations`), `app/api/v1/chat.py` (`list_conversations`, `send_message`, `practice_action`, archive/unarchive routes)
- Test: `tests/test_removal.py`

**Interfaces:**
- Produces: `async def set_conversation_archived(session, learner_id, conversation_id, *, archived: bool) -> Conversation | None`; routes `POST /conversations/{id}/archive|unarchive` → `ConversationRead`; `GET /conversations?archived=true`.

- [ ] **Step 1: Failing test** — append:

```python
async def test_an_archived_conversation_is_listed_apart_and_read_only(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.commit()
    cid = str(conversation.id)

    r = await api_client.post(f"{API}/conversations/{cid}/archive")
    assert r.status_code == 200 and r.json()["archived_at"] is not None
    assert cid not in [c["id"] for c in (await api_client.get(f"{API}/conversations")).json()]
    archived = (await api_client.get(f"{API}/conversations?archived=true")).json()
    assert [c["id"] for c in archived] == [cid]
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "hello"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "archived"
    r = await api_client.post(f"{API}/conversations/{cid}/practice", json={"action": "skip"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "archived"
    assert (await api_client.get(f"{API}/conversations/{cid}/messages")).status_code == 200

    r = await api_client.post(f"{API}/conversations/{cid}/unarchive")
    assert r.status_code == 200 and r.json()["archived_at"] is None
    assert (await api_client.post(f"{API}/conversations/{uuid.uuid4()}/archive")).status_code == 404
```

(Match `ChatTurnRequest` / `PracticeActionSubmit` field names in `app/schemas/chat.py` if they differ.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_removal.py -q -k conversation` → FAIL (405/404 on `/archive`).

- [ ] **Step 3: Implement.**

`removal.py`:

```python
async def set_conversation_archived(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID, *, archived: bool
) -> Conversation | None:
    """Archive or unarchive a conversation: out of the list and read-only, memories untouched."""
    conversation = await chat_svc.get_conversation(session, conversation_id, learner_id=learner_id)
    if conversation is None or conversation.learner_id != learner_id:
        return None
    conversation.archived_at = datetime.now(UTC) if archived else None
    await session.commit()
    return await chat_svc.get_conversation(session, conversation_id, learner_id=learner_id)
```

(imports: `from app.models.chat import Conversation`, `from app.services import chat as chat_svc`.)

`app/services/chat.py` `list_conversations(session, learner_id, *, archived: bool = False)`: add `.where(Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None))`.

`app/api/v1/chat.py`:
- `list_conversations` takes `archived: Annotated[bool, Query()] = False` and passes it through.
- A helper beside the routes:

```python
def _refuse_if_archived(conversation: Conversation) -> None:
    """An archived conversation is read-only until unarchived (S61)."""
    if conversation.archived_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "archived", "message": "Unarchive this conversation to continue it."},
        )
```

  called in `send_message` and `practice_action` right after the 404 check.
- Routes `POST /conversations/{conversation_id}/archive` and `/unarchive` returning `ConversationRead`, 404 on `None`, mirroring the source routes.

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_removal.py tests/test_chat.py -q` → PASS.
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/removal.py app/services/chat.py app/api/v1/chat.py frontend/src/api/schema.d.ts tests/test_removal.py
git status
git commit -m "feat(chat): archive a conversation out of the list, read-only [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Impact and deletion [S61]

**Files:**
- Modify: `app/services/removal.py`, `app/schemas/source.py` (add `RemovalImpactRead`), `app/api/v1/sources.py`, `app/api/v1/chat.py` (`delete_conversation`)
- Test: `tests/test_removal.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Impact:
      kept: dict[str, int]         # keys: "lessons", "cited_replies" (source); "memories" (conversation)
      forgettable: dict[str, int]  # keys: "lessons" (source); "memories" (conversation)
      notes: list[str]
  async def source_impact(session, learner_id, source_id) -> Impact | None
  async def conversation_impact(session, learner_id, conversation_id) -> Impact | None
  async def delete_source(session, blobstore, learner_id, source_id, *, forget: bool) -> Impact | None
  async def delete_conversation(session, learner_id, conversation_id, *, forget: bool) -> Impact | None
  ```
  `RemovalImpactRead(BaseModel)`: `kept: dict[str, int]`, `forgettable: dict[str, int]`, `notes: list[str]`.
  Routes: `GET /sources/{id}/removal`, `DELETE /sources/{id}?forget=`, `GET /conversations/{id}/removal`, `DELETE /conversations/{id}?forget=` (now 200 with `RemovalImpactRead`).
  In this task `forget=True` is accepted and passed through but only its source half (lessons) is implemented; memories are Task 5.

- [ ] **Step 1: Failing tests** — append (imports: `from app.models.content import ContentBlock`; `from app.models.learning import LearnerKCState`, if needed):

```python
async def _lesson_citing(session: AsyncSession, learner: Learner, source_id: uuid.UUID) -> uuid.UUID:
    chunk_id = (await _current(session, source_id))[0]
    block = ContentBlock(
        learner_id=learner.id,
        kc_ids=[],
        block_type="lesson",
        body="Photosynthesis happens in chloroplasts.",
        citations=[{"chunk_id": str(chunk_id), "source_id": str(source_id)}],
        cache_key=f"k-{uuid.uuid4().hex}",
        model="fake-1",
    )
    session.add(block)
    await session.flush()
    return block.id


async def test_the_impact_says_what_a_delete_keeps(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source_id = await _source(db_session, learner)
    await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    impact = await removal.source_impact(db_session, learner.id, source_id)

    assert impact is not None
    assert impact.kept == {"lessons": 1, "cited_replies": 0}
    assert impact.forgettable == {"lessons": 1}
    assert any("evidence of what you can do" in note for note in impact.notes)


async def test_a_plain_delete_keeps_the_lessons_and_removes_the_file(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    source_id = await _source(db_session, learner, store=store)
    key = (await _get(db_session, source_id)).blob_key
    block_id = await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, source_id, forget=False)

    assert await db_session.get(Source, source_id) is None
    assert await db_session.get(ContentBlock, block_id) is not None
    assert key is not None and not await store.exists(key)


async def test_forget_also_removes_the_lessons_built_on_it(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    source_id = await _source(db_session, learner, store=store)
    block_id = await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, source_id, forget=True)

    assert await db_session.get(ContentBlock, block_id) is None


async def test_a_shared_file_survives_deleting_one_of_its_sources(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    first = await _source(db_session, learner, store=store)
    second = await _source(db_session, learner, store=store)  # same bytes → same blob key
    key = (await _get(db_session, first)).blob_key
    assert key == (await _get(db_session, second)).blob_key
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, first, forget=False)

    assert key is not None and await store.exists(key)


async def test_deleting_an_original_releases_its_duplicate(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    original = await _source(db_session, learner, PHOTO, store=store)
    dup = await _source(db_session, learner, PHOTO_US, store=store)
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, original, forget=False)

    assert dup in await ingestion.stranded_duplicates(db_session)


async def test_a_source_mid_ingest_cannot_be_deleted(db_session: AsyncSession) -> None:
    import pytest
    from sqlalchemy import func

    learner = await _learner(db_session)
    source_id = await _source(db_session, learner)
    source = await _get(db_session, source_id)
    source.status = SourceStatus.PROCESSING
    source.lease_expires_at = await db_session.scalar(select(func.now() + func.make_interval(0, 0, 0, 0, 1)))
    await db_session.commit()

    with pytest.raises(removal.RemovalRefused) as refused:
        await removal.delete_source(db_session, InMemoryBlobStore(), learner.id, source_id, forget=False)
    assert refused.value.code == "ingesting"


async def test_the_api_deletes_and_says_what_it_kept(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    source_id = await _source(db_session, api_learner)
    await _lesson_citing(db_session, api_learner, source_id)
    await db_session.commit()

    impact = await api_client.get(f"{API}/sources/{source_id}/removal")
    assert impact.status_code == 200 and impact.json()["kept"]["lessons"] == 1
    r = await api_client.delete(f"{API}/sources/{source_id}")
    assert r.status_code == 200 and r.json()["kept"]["lessons"] == 1
    assert (await api_client.delete(f"{API}/sources/{source_id}")).status_code == 404
    assert (await api_client.get(f"{API}/sources/{source_id}/removal")).status_code == 404


async def test_deleting_a_conversation_keeps_its_memories_and_their_origin(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.flush()
    memory = await _memory(db_session, api_learner, conversation, "Studies in the mornings")
    cid, mid = conversation.id, memory.id
    await db_session.commit()

    r = await api_client.delete(f"{API}/conversations/{cid}")

    assert r.status_code == 200 and r.json()["kept"] == {"memories": 1}
    kept = await db_session.get(Memory, mid, populate_existing=True)
    assert kept is not None and kept.status == MemoryStatus.CURRENT
    assert kept.conversation_id is None and kept.origin_conversation_id == cid
    assert (await api_client.delete(f"{API}/conversations/{cid}")).status_code == 404
```

(`InMemoryBlobStore` — check its method for existence; if it has no `exists`, use whatever it offers, e.g. `key in store._blobs`, or `await store.get(key)` raising. Use `datetime.now(UTC) + timedelta(minutes=1)` for the lease if simpler than `make_interval`.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_removal.py -q` → FAIL (`AttributeError: module 'app.services.removal' has no attribute 'source_impact'`).

- [ ] **Step 3: Implement** in `removal.py` (imports: `from dataclasses import dataclass`; `from sqlalchemy import delete, func, select, update`; `from app.models.chat import Conversation, Message`; `from app.models.content import ContentBlock`; `from app.models.memory import Memory, MemoryStatus`; `from app.models.profile import LearnerProfile`; `from app.models.source import SourceStatus`; `from app.services import ingestion`; `from app.storage import BlobStore`; `import structlog`, `log = structlog.get_logger()`):

```python
_PROGRESS_NOTE = "Your answers and progress stay — they are evidence of what you can do."


@dataclass(frozen=True)
class Impact:
    """What removing something keeps, and what "also forget" would take with it."""

    kept: dict[str, int]
    forgettable: dict[str, int]
    notes: list[str]


# --- provenance: what derives from what (the only place this is decided) --------------------


def _lessons_citing(learner_id: uuid.UUID, source_id: uuid.UUID):
    """Lessons and other content blocks built on this source's passages."""
    return select(ContentBlock.id).where(
        ContentBlock.learner_id == learner_id,
        ContentBlock.citations.contains([{"source_id": str(source_id)}]),
    )


def _replies_citing(learner_id: uuid.UUID, source_id: uuid.UUID):
    """Chat replies that cite it — kept always; they show the passage as no longer available."""
    return (
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.learner_id == learner_id,
            Message.citations.contains([{"source_id": str(source_id)}]),
        )
    )


def _memories_from(learner_id: uuid.UUID, conversation_id: uuid.UUID):
    """Current memories learned in this conversation, live, archived or deleted."""
    return select(Memory.id).where(
        Memory.learner_id == learner_id,
        Memory.origin_conversation_id == conversation_id,
        Memory.status == MemoryStatus.CURRENT,
    )


async def _count(session: AsyncSession, stmt) -> int:
    return (await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0


# --- impact -----------------------------------------------------------------------------------


async def _source_impact(session: AsyncSession, source: Source) -> Impact:
    lessons = await _count(session, _lessons_citing(source.learner_id, source.id))
    replies = await _count(session, _replies_citing(source.learner_id, source.id))
    notes = [_PROGRESS_NOTE]
    if replies:
        notes.append("Replies that cited this file will show it as no longer available.")
    if source.subject_id is not None:
        notes.append("The subject this file belongs to stays.")
    return Impact(
        kept={"lessons": lessons, "cited_replies": replies},
        forgettable={"lessons": lessons},
        notes=notes,
    )


async def _conversation_impact(session: AsyncSession, conversation: Conversation) -> Impact:
    memories = await _count(session, _memories_from(conversation.learner_id, conversation.id))
    notes = [_PROGRESS_NOTE]
    if memories:
        notes.append(
            "Memories from this conversation stay unless you forget them; you can also forget "
            "them later from Memory."
        )
    return Impact(kept={"memories": memories}, forgettable={"memories": memories}, notes=notes)


async def source_impact(
    session: AsyncSession, learner_id: uuid.UUID, source_id: uuid.UUID
) -> Impact | None:
    source = await _own_source(session, learner_id, source_id)
    return None if source is None else await _source_impact(session, source)


async def conversation_impact(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID
) -> Impact | None:
    conversation = await _own_conversation(session, learner_id, conversation_id)
    return None if conversation is None else await _conversation_impact(session, conversation)


async def _own_conversation(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    conversation = await session.get(Conversation, conversation_id, populate_existing=True)
    return conversation if conversation is not None and conversation.learner_id == learner_id else None


async def _clear_profile_watermark(session: AsyncSession, learner_id: uuid.UUID) -> None:
    """The next profile refresh recomputes from what remains instead of reporting no change."""
    await session.execute(
        update(LearnerProfile)
        .where(LearnerProfile.learner_id == learner_id)
        .values(evidence_watermark=None)
    )


# --- delete -----------------------------------------------------------------------------------


async def delete_source(
    session: AsyncSession,
    blobstore: BlobStore,
    learner_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    forget: bool,
) -> Impact | None:
    """Delete a source now. Returns what was kept (and, with ``forget``, what went).

    Chunks — cited history too — KC tags and conversation links cascade; a duplicate's link
    nulls and the recovery sweep releases it (S77). The file goes after the commit, and only
    when no other source shares it; a store failure is reported, not raised — the database is
    already consistent, and an orphaned file is recoverable where a half-deleted source is not.
    """
    source = await _own_source(session, learner_id, source_id)
    if source is None:
        return None
    if source.status == SourceStatus.PROCESSING and source.lease_expires_at is not None:
        if await session.scalar(select(func.now() < source.lease_expires_at)):
            raise RemovalRefused("ingesting", "This source is being processed right now.")
    impact = await _source_impact(session, source)
    blob_key = source.blob_key
    if forget:
        await session.execute(
            delete(ContentBlock).where(
                ContentBlock.id.in_(_lessons_citing(learner_id, source_id))
            )
        )
        await _clear_profile_watermark(session, learner_id)
    await session.execute(delete(Source).where(Source.id == source_id))
    await session.commit()
    notes = list(impact.notes)
    if blob_key is not None:
        try:
            await ingestion.unreference_blob(session, blobstore, blob_key)
        except Exception:
            log.warning("removal.blob_not_deleted", source_id=str(source_id), exc_info=True)
            notes.append("The stored file could not be removed yet; it will be cleaned up.")
    return Impact(kept=impact.kept, forgettable=impact.forgettable, notes=notes)


async def delete_conversation(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID, *, forget: bool
) -> Impact | None:
    """Delete a conversation now; its memories keep their origin (Task 5 adds ``forget``)."""
    conversation = await _own_conversation(session, learner_id, conversation_id)
    if conversation is None:
        return None
    impact = await _conversation_impact(session, conversation)
    await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await session.commit()
    return impact
```

(Use `session.execute(delete(Conversation)...)` — the DB cascades messages and turns and nulls memories' FK; the ORM path in `chat_svc.delete_conversation` would do the same but needs the relationship loaded. Keep `chat_svc.delete_conversation` for any other caller; if it has none after this change, delete it.)

`app/schemas/source.py`:

```python
class RemovalImpactRead(BaseModel):
    """What removing a source or conversation keeps, and what "also forget" takes (S61, V11)."""

    kept: dict[str, int]
    forgettable: dict[str, int]
    notes: list[str]
```

`app/api/v1/sources.py` (import `BlobStoreDep` if not already, `RemovalImpactRead`, `from dataclasses import asdict`):

```python
@router.get("/sources/{source_id}/removal", response_model=RemovalImpactRead)
async def source_removal(source_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """What deleting this source would keep, and what forgetting would also remove."""
    impact = await removal.source_impact(session, learner.id, source_id)
    if impact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return asdict(impact)


@router.delete("/sources/{source_id}", response_model=RemovalImpactRead)
async def delete_source(
    source_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    blobstore: BlobStoreDep,
    forget: Annotated[bool, Query()] = False,
):
    """Delete a source now (S61). With ``forget``, the lessons built on it go too (V11)."""
    try:
        impact = await removal.delete_source(
            session, blobstore, learner.id, source_id, forget=forget
        )
    except removal.RemovalRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "message": exc.message}
        ) from exc
    if impact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
    return asdict(impact)
```

`app/api/v1/chat.py`: replace `delete_conversation` with the same shape (`GET /conversations/{id}/removal` and `DELETE /conversations/{id}?forget=` returning `RemovalImpactRead`, status 200, 404 on `None`).

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_removal.py tests/test_chat.py tests/test_sources_api.py tests/test_blob_sharing.py tests/test_retention.py -q` → PASS (update any existing test asserting `DELETE /conversations/{id}` returns 204 to expect 200).
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/removal.py app/schemas/source.py app/api/v1/sources.py app/api/v1/chat.py app/services/chat.py frontend/src/api/schema.d.ts tests/test_removal.py
git status
git commit -m "feat(sources): delete a source or conversation, saying first what stays [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Stage any existing test file you had to update for the 204 → 200 change.)

---

### Task 5: Forgetting what a conversation taught [S42]

**Files:**
- Modify: `app/services/removal.py` (`delete_conversation` forget branch, `forget_conversation_memories`), `app/api/v1/memory.py` (`forget-origin` route, list route origin fields), `app/schemas/memory.py` (`MemoryRead`, `ForgetOriginRead`)
- Test: `tests/test_removal.py`

**Interfaces:**
- Produces: `async def forget_conversation_memories(session, learner_id, conversation_id) -> int`; `POST /memory/forget-origin/{conversation_id}` → `ForgetOriginRead(forgotten: int)`; `MemoryRead.origin_conversation_id: uuid.UUID | None`, `MemoryRead.origin_title: str | None`, `MemoryRead.origin_live: bool`.

- [ ] **Step 1: Failing tests** — append:

```python
async def test_forgetting_a_conversation_forgets_its_memories_for_good(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    learned = await _memory(db_session, learner, conversation, "Studies in the mornings")
    written = await _memory(db_session, learner, None, "Prefers worked examples")
    cid, learned_id, written_id = conversation.id, learned.id, written.id
    await db_session.commit()

    await removal.delete_conversation(db_session, learner.id, cid, forget=True)

    assert (await db_session.get(Memory, learned_id, populate_existing=True)).status == (
        MemoryStatus.DELETED
    )
    assert (await db_session.get(Memory, written_id, populate_existing=True)).status == (
        MemoryStatus.CURRENT
    ), "a memory the learner wrote has no origin and is never swept"


async def test_forget_by_origin_works_after_the_conversation_is_gone(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    conversation = Conversation(learner_id=api_learner.id, title="Chem help")
    db_session.add(conversation)
    await db_session.flush()
    memory = await _memory(db_session, api_learner, conversation, "Studies in the mornings")
    cid, mid = conversation.id, memory.id
    await db_session.commit()

    listed = (await api_client.get(f"{API}/memory")).json()
    assert listed[0]["origin_title"] == "Chem help" and listed[0]["origin_live"] is True
    await api_client.delete(f"{API}/conversations/{cid}")
    listed = (await api_client.get(f"{API}/memory")).json()
    assert listed[0]["origin_live"] is False and listed[0]["origin_conversation_id"] == str(cid)

    r = await api_client.post(f"{API}/memory/forget-origin/{cid}")
    assert r.status_code == 200 and r.json() == {"forgotten": 1}
    again = await api_client.post(f"{API}/memory/forget-origin/{cid}")
    assert again.json() == {"forgotten": 0}
    gone = await db_session.get(Memory, mid, populate_existing=True)
    assert gone is not None and gone.status == MemoryStatus.DELETED


async def test_forgetting_clears_the_profile_watermark(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime

    from app.models.profile import LearnerProfile

    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    db_session.add(LearnerProfile(learner_id=learner.id, evidence_watermark=datetime.now(UTC)))
    await db_session.flush()
    await _memory(db_session, learner, conversation, "Studies in the mornings")
    cid = conversation.id
    await db_session.commit()

    await removal.forget_conversation_memories(db_session, learner.id, cid)

    profile = await db_session.scalar(
        select(LearnerProfile)
        .where(LearnerProfile.learner_id == learner.id)
        .execution_options(populate_existing=True)
    )
    assert profile is not None and profile.evidence_watermark is None


async def test_forgetting_leaves_mastery_alone(db_session: AsyncSession) -> None:
    from app.models.learning import LearningEvent

    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    db_session.add(LearningEvent(learner_id=learner.id, event_type="answer", payload={}))
    await db_session.flush()
    await _memory(db_session, learner, conversation, "Studies in the mornings")
    cid = conversation.id
    await db_session.commit()

    await removal.delete_conversation(db_session, learner.id, cid, forget=True)

    count = await db_session.scalar(
        select(func.count()).select_from(LearningEvent).where(LearningEvent.learner_id == learner.id)
    )
    assert count == 1
```

(import `func` from sqlalchemy at the top of the test file. If `LearningEvent` needs other required fields, read `app/models/learning.py` and supply them.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_removal.py -q -k "forget"` → FAIL.

- [ ] **Step 3: Implement.** `removal.py`:

```python
async def forget_conversation_memories(
    session: AsyncSession, learner_id: uuid.UUID, conversation_id: uuid.UUID
) -> int:
    """Forget what a conversation taught — live, archived or already deleted (S42, V11).

    The existing soft delete, so re-extraction recognises each fact and does not bring it back.
    Memories the learner wrote themselves have no origin and are never touched. Idempotent: a
    second call finds nothing current and returns 0.
    """
    ids = list((await session.scalars(_memories_from(learner_id, conversation_id))).all())
    if ids:
        await session.execute(
            update(Memory).where(Memory.id.in_(ids)).values(status=MemoryStatus.DELETED)
        )
        await _clear_profile_watermark(session, learner_id)
    await session.commit()
    return len(ids)
```

In `delete_conversation`, before the delete statement:

```python
    if forget:
        await session.execute(
            update(Memory)
            .where(Memory.id.in_(_memories_from(learner_id, conversation_id)))
            .values(status=MemoryStatus.DELETED)
        )
        await _clear_profile_watermark(session, learner_id)
```

and update its docstring ("With ``forget``, the memories it taught are forgotten too.").

`app/schemas/memory.py`:

```python
class MemoryRead(BaseModel):
    ...existing fields...
    # Where it was learned (S42): the conversation id even after that conversation is deleted,
    # its title while it exists, and whether it still does.
    origin_conversation_id: uuid.UUID | None = None
    origin_title: str | None = None
    origin_live: bool = False


class ForgetOriginRead(BaseModel):
    forgotten: int
```

`app/api/v1/memory.py`:

```python
@router.get("/memory", response_model=list[MemoryRead])
async def list_memory(
    session: SessionDep,
    learner: CurrentLearner,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    memories = await svc.list_memories(session, learner.id, limit=limit)
    origins = {m.origin_conversation_id for m in memories if m.origin_conversation_id}
    live = {
        c.id: c.title or c.goal
        for c in (
            await session.scalars(
                select(Conversation).where(
                    Conversation.id.in_(origins), Conversation.learner_id == learner.id
                )
            )
        ).all()
    }
    return [
        MemoryRead.model_validate(m).model_copy(
            update={
                "origin_title": live.get(m.origin_conversation_id),
                "origin_live": m.origin_conversation_id in live,
            }
        )
        for m in memories
    ]


@router.post("/memory/forget-origin/{conversation_id}", response_model=ForgetOriginRead)
async def forget_origin(conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Forget every memory learned in one conversation, even one already deleted (S42)."""
    return ForgetOriginRead(
        forgotten=await removal.forget_conversation_memories(session, learner.id, conversation_id)
    )
```

(imports: `from sqlalchemy import select`, `from app.models.chat import Conversation`, `from app.services import removal`, `ForgetOriginRead`.) A stranger's conversation id simply matches none of the caller's memories → `{"forgotten": 0}`; no existence is revealed.

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_removal.py tests/test_memory_lifecycle.py tests/test_authorization.py -q` → PASS.
Run: `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/removal.py app/api/v1/memory.py app/schemas/memory.py frontend/src/api/schema.d.ts tests/test_removal.py
git status
git commit -m "feat(memory): forget what a conversation taught, even after it is deleted [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Frontend — menus, dialog, archived sections, memory origin [S61, S42]

**Files:**
- Create: `frontend/src/components/removal/RemovalDialog.tsx`, `frontend/src/components/removal/removalCopy.ts`, `frontend/src/components/removal/removalCopy.test.ts`, `frontend/src/components/removal/RemovalDialog.test.tsx`
- Modify: `frontend/src/api/hooks.ts`, `frontend/src/components/uploads/SourceList.tsx`, `frontend/src/pages/Uploads.tsx`, `frontend/src/components/chat/ConversationRow.tsx`, `frontend/src/components/chat/ConversationSidebar.tsx`, `frontend/src/hooks/useChatConversation.ts`, `frontend/src/pages/Chat.tsx`, `frontend/src/pages/Memory.tsx`, `frontend/src/pages/Memory.test.tsx`

**Interfaces:**
- Consumes: routes from Tasks 2–5; `SourceRead.archived_at`, `ConversationRead.archived_at`, `MemoryRead.origin_*`, `RemovalImpactRead`.
- Produces: `RemovalDialog({ kind, id, name, open, onClose, onDeleted })`; `describeCounts(counts, copy): string[]`.

- [ ] **Step 1: Failing tests.** `removalCopy.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { KEPT_COPY, describeCounts } from "./removalCopy";

describe("describeCounts", () => {
  it("names each non-zero count, singular and plural", () => {
    expect(describeCounts({ lessons: 1, cited_replies: 3 }, KEPT_COPY)).toEqual([
      "1 lesson built from this",
      "3 replies that cite this",
    ]);
  });

  it("leaves out what is zero", () => {
    expect(describeCounts({ lessons: 0, memories: 0 }, KEPT_COPY)).toEqual([]);
  });
});
```

`RemovalDialog.test.tsx` renders the dialog with a mocked impact (mock `../../api/hooks`' `useRemovalImpact` and `useRemove` with `vi.mock`), asserting: the kept lines and notes show; the forget checkbox lists "1 lesson built from this"; clicking Delete calls the mutation with `{ id, forget: false }`, and with the box ticked `{ id, forget: true }`; a 409 error with `{detail: {code: "ingesting", message: "…"}}` shows its message.

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const mutate = vi.fn();
vi.mock("../../api/hooks", () => ({
  useRemovalImpact: () => ({
    data: {
      kept: { lessons: 1, cited_replies: 0 },
      forgettable: { lessons: 1 },
      notes: ["Your answers and progress stay — they are evidence of what you can do."],
    },
    isLoading: false,
  }),
  useRemove: () => ({ mutate, isPending: false, error: null }),
}));

import { RemovalDialog } from "./RemovalDialog";

describe("RemovalDialog", () => {
  it("says what stays and deletes with or without forgetting", () => {
    render(
      <RemovalDialog kind="source" id="s1" name="notes.pdf" open onClose={() => {}} />,
    );
    expect(screen.getAllByText("1 lesson built from this").length).toBeGreaterThan(0);
    expect(screen.getByText(/evidence of what you can do/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mutate).toHaveBeenLastCalledWith({ id: "s1", forget: false }, expect.anything());

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mutate).toHaveBeenLastCalledWith({ id: "s1", forget: true }, expect.anything());
  });
});
```

In `Memory.test.tsx` add a case: a memory with `origin_live: false, origin_conversation_id: "c1"` renders "From a deleted conversation" and a "Forget all from this conversation" button; one with `origin_title: "Chem help", origin_live: true` renders "From: Chem help". (Follow that file's existing mocking pattern.)

- [ ] **Step 2: Run to verify failure** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/removal src/pages/Memory.test.tsx` → FAIL (modules not found).

- [ ] **Step 3: Implement.**

`removalCopy.ts`:

```ts
/** Plain-language lines for what a removal keeps or forgets (S61, V11). */
type Copy = Record<string, (n: number) => string>;

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export const KEPT_COPY: Copy = {
  lessons: (n) => `${plural(n, "lesson", "lessons")} built from this`,
  cited_replies: (n) => `${plural(n, "reply", "replies")} that cite this`,
  memories: (n) => `${plural(n, "memory", "memories")} from this conversation`,
};

export const FORGET_COPY: Copy = KEPT_COPY;

export function describeCounts(counts: Record<string, number>, copy: Copy): string[] {
  return Object.entries(counts)
    .filter(([key, n]) => n > 0 && key in copy)
    .map(([key, n]) => copy[key](n));
}
```

`hooks.ts` additions:

```ts
type RemovalKind = "source" | "conversation";

/** Archived sources, for the Uploads page's Archived section (S61). */
export function useArchivedSources() {
  return useQuery({
    queryKey: ["sources", "archived"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/sources", {
        params: { query: { archived: true } },
      });
      if (error) throw error;
      return data;
    },
  });
}

export function useArchivedConversations() {
  return useQuery({
    queryKey: ["conversations", "archived"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/conversations", {
        params: { query: { archived: true } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** Archive or unarchive; nothing is deleted either way (S61). */
export function useArchive(kind: RemovalKind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, archived }: { id: string; archived: boolean }) => {
      const verb = archived ? "archive" : "unarchive";
      const { error } =
        kind === "source"
          ? await api.POST(`/api/v1/sources/{source_id}/${verb}`, {
              params: { path: { source_id: id } },
            })
          : await api.POST(`/api/v1/conversations/{conversation_id}/${verb}`, {
              params: { path: { conversation_id: id } },
            });
      if (error) throw error;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [kind === "source" ? "sources" : "conversations"] });
    },
  });
}

/** What deleting would keep and what forgetting would also remove — read before deleting. */
export function useRemovalImpact(kind: RemovalKind, id: string, enabled: boolean) {
  return useQuery({
    queryKey: ["removal", kind, id],
    enabled,
    queryFn: async () => {
      const { data, error } =
        kind === "source"
          ? await api.GET("/api/v1/sources/{source_id}/removal", {
              params: { path: { source_id: id } },
            })
          : await api.GET("/api/v1/conversations/{conversation_id}/removal", {
              params: { path: { conversation_id: id } },
            });
      if (error) throw error;
      return data;
    },
  });
}

export function useRemove(kind: RemovalKind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, forget }: { id: string; forget: boolean }) => {
      const { data, error } =
        kind === "source"
          ? await api.DELETE("/api/v1/sources/{source_id}", {
              params: { path: { source_id: id }, query: { forget } },
            })
          : await api.DELETE("/api/v1/conversations/{conversation_id}", {
              params: { path: { conversation_id: id }, query: { forget } },
            });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      for (const key of ["sources", "conversations", "memories", "removal"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
  });
}

export function useForgetOrigin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (conversationId: string) => {
      const { data, error } = await api.POST("/api/v1/memory/forget-origin/{conversation_id}", {
        params: { path: { conversation_id: conversationId } },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memories"] });
    },
  });
}
```

If openapi-fetch's typed paths reject the template-literal `verb` paths, write the four archive/unarchive calls out explicitly. Replace `useDeleteConversation` with `useRemove("conversation")` at its call sites and delete it.

`RemovalDialog.tsx` — a daisyUI `<dialog className="modal" open={open}>` (match `NewChatModal.tsx`'s modal markup):

```tsx
import { useState } from "react";
import { useRemovalImpact, useRemove } from "../../api/hooks";
import { FORGET_COPY, KEPT_COPY, describeCounts } from "./removalCopy";

type Kind = "source" | "conversation";

function refusal(error: unknown): string | null {
  const detail = (error as { detail?: { message?: string } } | null)?.detail;
  return detail?.message ?? (error ? "That didn't work. Try again." : null);
}

/** Delete now, after saying what stays — and optionally forget what was learned (S61, V11). */
export function RemovalDialog({
  kind,
  id,
  name,
  open,
  onClose,
  onDeleted,
}: {
  kind: Kind;
  id: string;
  name: string;
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const [forget, setForget] = useState(false);
  const impact = useRemovalImpact(kind, id, open);
  const remove = useRemove(kind);
  const kept = describeCounts(impact.data?.kept ?? {}, KEPT_COPY);
  const forgettable = describeCounts(impact.data?.forgettable ?? {}, FORGET_COPY);
  const message = refusal(remove.error);

  return (
    <dialog className="modal" open={open}>
      <div className="modal-box flex flex-col gap-3">
        <h3 className="text-h3">Delete {name}?</h3>
        {impact.isLoading ? (
          <p className="text-caption text-base-content/50">Checking what this affects…</p>
        ) : (
          <>
            {kept.length > 0 && (
              <div>
                <p className="text-body">These stay:</p>
                <ul className="text-caption list-disc pl-5">
                  {kept.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </div>
            )}
            {(impact.data?.notes ?? []).map((note) => (
              <p key={note} className="text-caption text-base-content/70">
                {note}
              </p>
            ))}
            {forgettable.length > 0 && (
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="checkbox checkbox-sm"
                  checked={forget}
                  onChange={(e) => setForget(e.target.checked)}
                />
                <span className="text-body">
                  Also forget what was learned from this
                  <span className="text-caption text-base-content/60 block">
                    Removes {forgettable.join(", ")}.
                  </span>
                </span>
              </label>
            )}
          </>
        )}
        {message && <p className="text-caption text-error">{message}</p>}
        <div className="modal-action">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-error btn-sm"
            disabled={remove.isPending || impact.isLoading}
            onClick={() =>
              remove.mutate({ id, forget }, { onSuccess: () => (onDeleted ?? onClose)() })
            }
          >
            Delete
          </button>
        </div>
      </div>
    </dialog>
  );
}
```

`SourceList.tsx`: `SourceRow` gains an Archive button (`useArchive("source")`, `{ id, archived: true }`) and a Delete… button opening `RemovalDialog kind="source"`. Add a prop `archived?: boolean` to `SourceList`; when set, rows show Unarchive instead of Archive and no retry actions. `Uploads.tsx` renders, below the list, a `<details>` "Archived (n)" containing `<SourceList sources={archivedSources} isLoading={…} archived />` from `useArchivedSources()`, only when n > 0.

`ConversationRow.tsx`: replace the two-click trash with Archive (or Unarchive for an archived row, via an `archived` prop) and a Delete… button opening `RemovalDialog kind="conversation"`, `onDeleted` navigating to `/app/chat` when active. `ConversationSidebar.tsx`: a `<details>` "Archived (n)" under the list using `useArchivedConversations()`, rows rendered with `archived`.

`useChatConversation.ts`: look the conversation up in both `useConversations()` and `useArchivedConversations()` data. `Chat.tsx`: when `conversation?.archived_at`, replace the `Composer` and practice controls with a banner — "This conversation is archived." plus an "Unarchive to continue" button (`useArchive("conversation")`).

`Memory.tsx`: under each memory's text, when `origin_conversation_id` is set, show `From: {origin_title ?? "an untitled conversation"}` if `origin_live`, else "From a deleted conversation", with a small "Forget all from this conversation" button (`useForgetOrigin()`), confirmed inline the way that page already confirms "Forget everything".

- [ ] **Step 4: Run tests and the build**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build`, `npx prettier --check` and `npx eslint` on changed files → green, no new warnings.

- [ ] **Step 5: Commit (two commits, one id each)**

```bash
git add frontend/src/api/hooks.ts frontend/src/components/removal frontend/src/components/uploads/SourceList.tsx frontend/src/pages/Uploads.tsx frontend/src/components/chat/ConversationRow.tsx frontend/src/components/chat/ConversationSidebar.tsx frontend/src/hooks/useChatConversation.ts frontend/src/pages/Chat.tsx
git status
git commit -m "feat(web): archive and delete sources and conversations, saying what stays [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git add frontend/src/pages/Memory.tsx frontend/src/pages/Memory.test.tsx
git status
git commit -m "feat(web): memories say where they were learned, and can be forgotten together [S42]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(`useForgetOrigin` lives in `hooks.ts`, which goes in the first commit; the second commit only uses it.)

---

### Task 7: Tracker and docs [S61]

**Files:** Modify `docs/guru-suggestions-tracker.md` (rows S61, S42), `CLAUDE.md` (Key Technical Decisions).

- [ ] **Step 1:** S61 → still **Partial**: "Archive/delete/forget done (V11): sources and conversations archive reversibly (never retrieved / read-only), delete immediately after an impact report of what stays, and optionally forget derived lessons or memories; memories keep their origin after a conversation is deleted. Remaining (slice B, V12): account deletion lifecycle with a seven-day recovery window and erase-now, selective 30-day diagnostic/backup retention, retryable orphan cleanup, export access to uploaded files." Add evidence links `[removal service](../app/services/removal.py)`, `[removal tests](../tests/test_removal.py)`.
- [ ] **Step 2:** S42 → **Partial**: "Deletion with optional forgetting done: a conversation's memories can be forgotten on delete or later by origin, without reviving on re-extraction. Remaining: replace similarity-only supersession with contradiction versus coexistence (slice D)."
- [ ] **Step 3:** `CLAUDE.md` Key Technical Decisions, after the S29/S50 bullet:

```markdown
- **Archive, delete and forget are three actions** (S61, S42; V11) — archive is reversible and
  out of use (retrieval drops archived sources, archived conversations are read-only); delete is
  immediate after an impact report of what stays; forget removes only what was derived — lessons
  built on a source, memories learned in a conversation — never answers or mastery.
  `app/services/removal.py` is the one place that decides what derives from what.
```

- [ ] **Step 4:** `uv run poe check` → green, then:

```bash
git add docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record archive, delete and forget in the tracker [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
