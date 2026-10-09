# Measured Long-History Performance (Part A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Budgets that fail when a hot path's statements or rows grow with a learner's history, a report that times every hot path against a 5× power-user history, and paging for the two lists that return everything.

**Architecture:** `tests/history.py` bulk-seeds a proportional history with set-based SQL. `tests/perf/paths.py` names each hot path once as a service call; `tests/test_history_budgets.py` runs every path at history ×1 and ×4 under a statement-and-row counter, and `tests/perf/report.py` (`poe perf-report`) seeds `guru_perf` and times the same paths. The conversation and source lists become cursor pages like the transcript, with a single-conversation read for the chat page.

**Tech Stack:** Python 3.13, SQLAlchemy async + asyncpg, Postgres (`generate_series`, pgvector), pytest; React + TanStack Query (`useInfiniteQuery`), openapi-typescript.

**Spec:** `docs/superpowers/specs/2026-10-09-long-history-performance-design.md`

## Global Constraints

- Branch `feat/workstream-2` (open PR #44). Never reset, amend, rebase, squash or force-push.
- Stage only the task's files by name, then `git status`.
- Commit subjects end `[S62]`; every commit message ends with the exact line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Never read or print `.env` or any secret; never set `GURU_JEV_SMOKE`; no paid model runs — every path and seed uses `fake_llm_client`.
- Fix rule (spec decision 3): fix a path when statements or rows grow with history, or when its p95 at the 5× history exceeds **250 ms**.
- Budget assertion: statements(×4) ≤ statements(×1) + 2 and rows(×4) ≤ rows(×1) + 2.
- Settings, verbatim: `chat_conversation_page_size: int = 50`, `chat_conversation_page_max: int = 200`, `sources_page_size: int = 100`, `sources_page_max: int = 500`.
- Page shape: `ConversationPage {conversations, has_more}`, `SourcePage {sources, has_more}`; cursor `before=<id>` on `(created_at, id)` descending; oversized `limit` clamped; unknown/foreign cursor → first page.
- The report's database is `guru_perf` (`tests.testdb --suffix _perf`); it never touches dev or test data. It always exits 0.
- Gates: `uv run poe check` (background, ~3 min), `uv run poe format-check`, `uv run poe api-contract`; frontend `npm run build`, `npm test -- --run`, `npm run lint` in `frontend/`.
- Every finished piece updates `docs/guru-suggestions-tracker.md` (Task 7).

## Review Focus

1. A learner with more conversations than one page opens an old conversation from a link or a reload: the chat page must still find it (it used to search the list). Pinned in Task 4 (`useChatConversation` reads `useConversation(id)`) and Task 2 (`test_a_conversation_is_readable_by_id`).
2. A stale or foreign cursor (another learner's conversation id, a deleted source) must yield the first page, never an error or another learner's rows. Pinned in Tasks 2 and 3.
3. The Uploads page polls while anything is ingesting; with paging it must keep polling every loaded page and stop when nothing is in flight. Pinned in Task 4 (`stillIngesting` reads the flattened pages).
4. Two rows with the same `created_at` (one transaction) must not be skipped or repeated across a page boundary. Pinned in Tasks 2 and 3 (`…ties_at_a_page_boundary…`).
5. A budget test must measure the path, not the seed: counting starts after seeding and both learners' histories coexist. Pinned in Task 5 (the counter wraps only the path call).

---

### Task 1: The history builder and a counter that counts rows

**Files:**
- Modify: `tests/querycount.py`
- Create: `tests/history.py`, `tests/test_history_builder.py`

**Interfaces:**
- Produces: `QueryCount.rows -> int` (sum of rowcounts, negatives ignored) and `QueryCount.entries: list[tuple[str, int]]`; `HistoryShape` (frozen dataclass) with `.scaled(k: int) -> HistoryShape`; `SMALL`, `POWER_USER`, `ORDINARY`; `SeededHistory(learner_id, subject_id, topic_id, conversation_id, practice_conversation_id)`; `async seed_history(session: AsyncSession, learner_id: uuid.UUID, shape: HistoryShape) -> SeededHistory`.

- [ ] **Step 1: Write the failing tests**

`tests/test_history_builder.py`:

```python
"""The history the S62 budgets and report run against is the size it says it is."""

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, LLMCall, Message, Turn
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.memory import Memory
from app.models.note import Note, NoteRevision
from app.models.source import Chunk, Source
from tests.history import SMALL, seed_history
from tests.querycount import count_queries


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"h-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _count(session: AsyncSession, column, learner_id: uuid.UUID) -> int:
    return await session.scalar(select(func.count()).where(column == learner_id)) or 0


async def test_the_small_history_has_the_stated_shape(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    h = await seed_history(db_session, learner.id, SMALL)

    # +1: the empty practice conversation
    assert await _count(db_session, Conversation.learner_id, learner.id) == SMALL.conversations + 1
    messages = await db_session.scalar(
        select(func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.learner_id == learner.id)
    )
    assert messages == SMALL.conversations * SMALL.messages_per_conversation
    turns = await db_session.scalar(
        select(func.count(Turn.id))
        .join(Conversation, Conversation.id == Turn.conversation_id)
        .where(Conversation.learner_id == learner.id)
    )
    assert turns == messages // 2
    assert await _count(db_session, LearningEvent.learner_id, learner.id) == SMALL.events
    kcs = SMALL.subjects * SMALL.topics_per_subject * SMALL.kcs_per_topic
    assert await _count(db_session, LearnerKCState.learner_id, learner.id) == kcs
    assert await _count(db_session, Memory.learner_id, learner.id) == SMALL.memories
    assert await _count(db_session, Source.learner_id, learner.id) == SMALL.sources
    chunks = await db_session.scalar(
        select(func.count(Chunk.id))
        .join(Source, Source.id == Chunk.source_id)
        .where(Source.learner_id == learner.id)
    )
    assert chunks == SMALL.sources * SMALL.chunks_per_source
    assert await _count(db_session, Note.learner_id, learner.id) == SMALL.topics_per_subject
    revisions = await db_session.scalar(
        select(func.count(NoteRevision.id))
        .join(Note, Note.id == NoteRevision.note_id)
        .where(Note.learner_id == learner.id)
    )
    assert revisions == SMALL.topics_per_subject * SMALL.note_revisions
    assert await _count(db_session, LLMCall.learner_id, learner.id) == SMALL.llm_calls
    plan = await db_session.scalar(
        select(LessonPlan).where(
            LessonPlan.learner_id == learner.id, LessonPlan.subject_id == h.subject_id
        )
    )
    assert plan is not None


def test_scaling_multiplies_history_but_not_the_graph() -> None:
    big = SMALL.scaled(4)
    assert big.conversations == 4 * SMALL.conversations
    assert big.messages_per_conversation == 4 * SMALL.messages_per_conversation
    assert big.events == 4 * SMALL.events
    assert big.kcs_per_topic == SMALL.kcs_per_topic
    assert big.subjects == SMALL.subjects


async def test_the_counter_counts_rows(db_session: AsyncSession) -> None:
    with count_queries(db_session) as counted:
        await db_session.execute(text("SELECT g FROM generate_series(1, 7) g"))
        await db_session.execute(text("SELECT 1 WHERE false"))
    assert len(counted) == 2
    assert counted.rows == 7
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_history_builder.py -q`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'tests.history'`.

- [ ] **Step 3: Implement**

`tests/querycount.py` — replace `QueryCount` and `count_queries` with:

```python
@dataclass
class QueryCount:
    entries: list[tuple[str, int]] = field(default_factory=list)

    @property
    def statements(self) -> list[str]:
        return [s for s, _ in self.entries]

    @property
    def rows(self) -> int:
        """Rows returned or affected, summed. A row count grows where a statement count does
        not: an unbounded list is one query whatever its size."""
        return sum(max(n, 0) for _, n in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:  # what a budget failure should print
        return f"QueryCount({len(self.entries)} statements, {self.rows} rows):\n" + "\n".join(
            f"  [{n:>6}] {s.split(chr(10))[0][:110]}" for s, n in self.entries
        )


@contextmanager
def count_queries(session: AsyncSession) -> Iterator[QueryCount]:
    """Count every statement executed on ``session``'s connection inside the block, with the
    rows each returned (``cursor.rowcount``, which asyncpg reports for SELECTs too)."""
    counted = QueryCount()
    sync_engine = session.get_bind().engine

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        # Transaction control is the harness's, not the operation's: the suite runs each test
        # inside a savepoint, and counting those would put a fixed offset on every budget.
        if not statement.lstrip().upper().startswith(_TRANSACTION_CONTROL):
            counted.entries.append((statement, cursor.rowcount))

    event.listen(sync_engine, "after_cursor_execute", _on_execute)
    try:
        yield counted
    finally:
        event.remove(sync_engine, "after_cursor_execute", _on_execute)
```

(Update the module docstring's second paragraph to say it counts rows as well.) Existing users only call `len(...)` and print the object, so they keep working.

`tests/history.py`:

```python
"""A learner's history at a stated size, seeded in bulk (S62).

Set-based SQL rather than the services: the report's power user has a hundred thousand
messages, and seeding them one service call at a time would take longer than everything it
measures. The shapes are what the services would have written — the columns the readers read
— not a replay of how they got there. Embeddings are random vectors in the fake client's
space, so no model is called.
"""

import json
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.services import lesson_plan as lesson_plan_svc
from tests.embedding import FAKE_SPACE

YEAR_SECONDS = 365 * 86400
NOTE_ATOMS = [
    {"id": "a-1", "kind": "concept", "kc_ids": [], "md": "Vectors add tip-to-tail.", "provenance": {}}
]


@dataclass(frozen=True)
class HistoryShape:
    conversations: int = 5
    messages_per_conversation: int = 40
    subjects: int = 1
    topics_per_subject: int = 4
    kcs_per_topic: int = 5
    events: int = 150
    memories: int = 30
    sources: int = 4
    chunks_per_source: int = 10
    note_revisions: int = 3
    llm_calls: int = 60

    def scaled(self, k: int) -> "HistoryShape":
        """``k`` times the history, on the same graph: history is evidence, not syllabus."""
        return replace(
            self,
            conversations=self.conversations * k,
            messages_per_conversation=self.messages_per_conversation * k,
            events=self.events * k,
            memories=self.memories * k,
            sources=self.sources * k,
            note_revisions=self.note_revisions * k,
            llm_calls=self.llm_calls * k,
        )


SMALL = HistoryShape()
POWER_USER = HistoryShape(
    conversations=2000,
    messages_per_conversation=50,
    subjects=15,
    topics_per_subject=30,
    kcs_per_topic=10,
    events=75_000,
    memories=15_000,
    sources=750,
    chunks_per_source=20,
    note_revisions=10,
    llm_calls=150_000,
)
ORDINARY = HistoryShape(
    conversations=200,
    messages_per_conversation=50,
    subjects=2,
    topics_per_subject=30,
    kcs_per_topic=10,
    events=7_500,
    memories=1_500,
    sources=75,
    chunks_per_source=20,
    note_revisions=10,
    llm_calls=15_000,
)


@dataclass(frozen=True)
class SeededHistory:
    learner_id: uuid.UUID
    subject_id: uuid.UUID  # the first subject; it has a lesson plan
    topic_id: uuid.UUID  # a topic of that subject with a note
    conversation_id: uuid.UUID  # the newest conversation, on the first subject
    practice_conversation_id: uuid.UUID  # empty, on the first subject


async def _ids(session: AsyncSession, sql: str, **params: object) -> list[uuid.UUID]:
    return list((await session.execute(text(sql), params)).scalars().all())


async def seed_history(
    session: AsyncSession, learner_id: uuid.UUID, shape: HistoryShape
) -> SeededHistory:
    """Seed ``shape`` for an existing learner and commit. Timestamps spread over the past year."""
    dim = get_settings().embed_dim
    p: dict[str, object] = {"learner": learner_id, "space": FAKE_SPACE, "dim": dim}

    subjects = await _ids(
        session,
        "INSERT INTO subjects (id, slug, name, owner_learner_id) "
        "SELECT gen_random_uuid(), 'h-' || md5(random()::text), 'History subject ' || g, :learner "
        "FROM generate_series(1, :n) g RETURNING id",
        n=shape.subjects,
        learner=learner_id,
    )
    first = subjects[0]
    p |= {"subjects": subjects, "ns": len(subjects), "first": first}
    await session.execute(
        text(
            "INSERT INTO topics (id, subject_id, slug, name) "
            "SELECT gen_random_uuid(), s, 't' || g, 'Topic ' || g "
            "FROM unnest(CAST(:subjects AS uuid[])) s, generate_series(1, :n) g"
        ),
        {"subjects": subjects, "n": shape.topics_per_subject},
    )
    await session.execute(
        text(
            "INSERT INTO kcs (id, topic_id, slug, name) "
            "SELECT gen_random_uuid(), t.id, 'k' || g, t.name || ' component ' || g "
            "FROM topics t, generate_series(1, :n) g "
            "WHERE t.subject_id = ANY(CAST(:subjects AS uuid[]))"
        ),
        {"subjects": subjects, "n": shape.kcs_per_topic},
    )
    kcs = await _ids(
        session,
        "SELECT k.id FROM kcs k JOIN topics t ON t.id = k.topic_id "
        "WHERE t.subject_id = ANY(CAST(:subjects AS uuid[])) "
        "ORDER BY (t.subject_id = :first) DESC, t.slug, k.slug",
        subjects=subjects,
        first=first,
    )
    p |= {"kcs": kcs, "nk": len(kcs)}

    # One bank item per component, so practice never asks a model for one. Deterministic ids
    # link the item to its component without a round trip.
    await session.execute(
        text(
            "INSERT INTO items (id, item_type, stem, difficulty, origin, owner_learner_id, "
            "author_learner_id) "
            "SELECT md5('item' || k)::uuid, 'short', 'Explain component ' || k || "
            "' in your own words.', 0.0, 'learner', :learner, :learner "
            "FROM unnest(CAST(:kcs AS uuid[])) k"
        ),
        p,
    )
    await session.execute(
        text(
            "INSERT INTO item_kcs (id, item_id, kc_id, weight) "
            "SELECT gen_random_uuid(), md5('item' || k)::uuid, k, 1.0 "
            "FROM unnest(CAST(:kcs AS uuid[])) k"
        ),
        p,
    )
    # Roughly a third due now: a review queue a long-standing learner actually has.
    await session.execute(
        text(
            "INSERT INTO learner_kc_state (id, learner_id, kc_id, ability, uncertainty, "
            "last_seen_at, due_at) "
            "SELECT gen_random_uuid(), :learner, k, random() * 2 - 1, 0.5, "
            "now() - interval '2 days', now() + (random() * 20 - 6) * interval '1 day' "
            "FROM unnest(CAST(:kcs AS uuid[])) k"
        ),
        p,
    )
    await session.execute(
        text(
            "INSERT INTO learning_events (id, learner_id, kc_id, event_type, attempt_id, "
            "payload, created_at) "
            "SELECT gen_random_uuid(), :learner, k, 'observation', gen_random_uuid(), "
            "jsonb_build_object('score', sc, 'item_score', sc, 'component_scored', false, "
            "'correct', sc >= 0.6, 'detail', null, 'diagnosis', null, 'response', "
            "'answer ' || g, 'hints_used', 0, 'prior_attempts', 0, 'item_id', "
            "md5('item' || k)::uuid::text, 'grading', null), "
            "timezone('utc', now()) - g * :step * interval '1 second' "
            "FROM (SELECT g, (CAST(:kcs AS uuid[]))[1 + g % :nk] AS k, "
            "round(random()::numeric, 2)::float8 AS sc FROM generate_series(1, :n) g) e"
        ),
        p | {"n": shape.events, "step": YEAR_SECONDS / max(shape.events, 1)},
    )
    conversations = await _ids(
        session,
        "INSERT INTO conversations (id, learner_id, subject_id, title, phase, created_at, "
        "updated_at) "
        "SELECT gen_random_uuid(), :learner, (CAST(:subjects AS uuid[]))[1 + (g - 1) % :ns], "
        "'Conversation ' || g, 'chatting', ts, ts "
        "FROM (SELECT g, timezone('utc', now()) - g * :step * interval '1 second' AS ts "
        "FROM generate_series(1, :n) g) x RETURNING id",
        **(p | {"n": shape.conversations, "step": YEAR_SECONDS / max(shape.conversations, 1)}),
    )
    p |= {"convs": conversations, "nc": len(conversations)}
    await session.execute(
        text(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) "
            "SELECT gen_random_uuid(), c.id, CASE WHEN m % 2 = 1 THEN 'user' ELSE 'assistant' END, "
            "'Message ' || m || ' of a long conversation about the subject.', "
            "c.created_at + m * interval '1 second' "
            "FROM conversations c, generate_series(1, :m) m WHERE c.id = ANY(CAST(:convs AS uuid[]))"
        ),
        p | {"m": shape.messages_per_conversation},
    )
    await session.execute(
        text(
            "INSERT INTO turns (id, conversation_id, flow, status, content, user_message_id, "
            "created_at, updated_at) "
            "SELECT gen_random_uuid(), m.conversation_id, 'tutor', 'completed', m.content, m.id, "
            "m.created_at, m.created_at FROM messages m "
            "WHERE m.conversation_id = ANY(CAST(:convs AS uuid[])) AND m.role = 'user'"
        ),
        p,
    )
    await session.execute(
        text(
            "INSERT INTO memories (id, learner_id, kind, content, embedding, embedding_space, "
            "status, origin_conversation_id, created_at, updated_at) "
            "SELECT gen_random_uuid(), :learner, 'fact', 'The learner remembers fact ' || g || '.', "
            "(SELECT array_agg(random() - 0.5 + g * 0) FROM generate_series(1, :dim))::vector, "
            ":space, 'current', (CAST(:convs AS uuid[]))[1 + g % :nc], ts, ts "
            "FROM (SELECT g, timezone('utc', now()) - g * :step * interval '1 second' AS ts "
            "FROM generate_series(1, :n) g) x"
        ),
        p | {"n": shape.memories, "step": YEAR_SECONDS / max(shape.memories, 1)},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, learner_id, subject_id, kind, origin, status, meta, "
            "attempts, content_type, created_at, updated_at) "
            "SELECT gen_random_uuid(), :learner, (CAST(:subjects AS uuid[]))[1 + (g - 1) % :ns], "
            "'file', 'document-' || g || '.txt', 'done', '{}'::jsonb, 1, 'text/plain', ts, ts "
            "FROM (SELECT g, timezone('utc', now()) - g * :step * interval '1 second' AS ts "
            "FROM generate_series(1, :n) g) x"
        ),
        p | {"n": shape.sources, "step": YEAR_SECONDS / max(shape.sources, 1)},
    )
    await session.execute(
        text(
            "INSERT INTO chunks (id, source_id, ordinal, text, tsv, provenance, embedding, "
            "embedding_space, created_at, updated_at) "
            "SELECT gen_random_uuid(), s.id, o, t.txt, to_tsvector('english', t.txt), "
            "jsonb_build_object('source_id', s.id::text, 'method', 'text'), "
            "(SELECT array_agg(random() - 0.5 + o * 0) FROM generate_series(1, :dim))::vector, "
            ":space, s.created_at, s.created_at "
            "FROM sources s, generate_series(0, :m - 1) o, "
            "LATERAL (SELECT 'Passage ' || o || ' of ' || s.origin || "
            "' explains component ' || (o % 7) || ' in detail.' AS txt) t "
            "WHERE s.learner_id = :learner"
        ),
        p | {"m": shape.chunks_per_source},
    )
    await session.execute(
        text(
            "INSERT INTO notes (id, learner_id, topic_id, substrate, revision_ordinal) "
            "SELECT gen_random_uuid(), :learner, t.id, CAST(:atoms AS jsonb), :r "
            "FROM topics t WHERE t.subject_id = :first"
        ),
        p | {"atoms": json.dumps(NOTE_ATOMS), "r": shape.note_revisions},
    )
    await session.execute(
        text(
            "INSERT INTO note_revisions (id, note_id, ordinal, substrate, cause) "
            "SELECT gen_random_uuid(), n.id, o, n.substrate, 'distill' "
            "FROM notes n, generate_series(1, :r) o WHERE n.learner_id = :learner"
        ),
        p | {"r": shape.note_revisions},
    )
    # Older than a day: a year of spend must not count against today's caps, or every turn the
    # report times would be refused.
    await session.execute(
        text(
            "INSERT INTO llm_calls (id, learner_id, conversation_id, role, provider, model, "
            "input_tokens, output_tokens, cost_usd, created_at) "
            "SELECT gen_random_uuid(), :learner, (CAST(:convs AS uuid[]))[1 + g % :nc], 'smart', "
            "'fake', 'fake-1', 800, 200, 0.0001, "
            "timezone('utc', now()) - interval '1 day' - g * :step * interval '1 second' "
            "FROM generate_series(1, :n) g"
        ),
        p | {"n": shape.llm_calls, "step": YEAR_SECONDS / max(shape.llm_calls, 1)},
    )
    await session.commit()

    await lesson_plan_svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner_id, subject_id=first, goal=None
    )
    practice = Conversation(learner_id=learner_id, subject_id=first, title="Practice")
    session.add(practice)
    await session.commit()
    topic_id = (
        await _ids(session, "SELECT id FROM topics WHERE subject_id = :first ORDER BY slug LIMIT 1", first=first)
    )[0]
    return SeededHistory(
        learner_id=learner_id,
        subject_id=first,
        topic_id=topic_id,
        conversation_id=conversations[0],
        practice_conversation_id=practice.id,
    )
```

(`conversations[0]` is the newest: `g = 1` is the most recent timestamp. Run `uv run ruff format tests/history.py`; long SQL strings may be re-wrapped.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_history_builder.py tests/test_query_budgets.py -q`
Expected: PASS. If a column value is rejected (an enum string, a NOT NULL without default), fix the SQL to what the model declares and ledger a ruling naming the column.

- [ ] **Step 5: Commit**

```bash
git add tests/querycount.py tests/history.py tests/test_history_builder.py
git commit -m "test(perf): a long history seeded in bulk, and a counter that counts rows [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: The conversation list pages; one conversation reads by id

**Files:**
- Modify: `app/core/config.py` (two settings beside `chat_transcript_page_max`)
- Modify: `app/services/chat.py:193-209` (`list_conversations`)
- Modify: `app/api/v1/chat.py:107-113` (`ConversationPage`, list endpoint, new `GET /conversations/{conversation_id}`)
- Modify tests reading the list as an array: `tests/test_authorization.py:70`, `tests/test_chat.py:156,200`, `tests/test_conversation_phase.py:132,144`, `tests/test_practice_pause.py:257`, `tests/test_removal.py:216-217`, `tests/test_workflow.py:603,615`
- Create: `tests/test_conversation_page.py`

**Interfaces:**
- Consumes: Task 1's `seed_history`, `SMALL`, `count_queries`.
- Produces: `async chat.list_conversations(session, learner_id, *, archived: bool = False, limit: int, before: uuid.UUID | None = None) -> tuple[list[Conversation], bool]`; `ConversationPage(conversations: list[ConversationRead], has_more: bool)`; `GET /api/v1/conversations?archived&limit&before` → `ConversationPage`; `GET /api/v1/conversations/{conversation_id}` → `ConversationRead` (404 when not the learner's).

- [ ] **Step 1: Write the failing tests**

`tests/test_conversation_page.py`:

```python
"""The conversation list is a page, not the whole history (S62)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation
from app.models.learner import Learner
from app.services import chat as chat_svc
from tests.history import SMALL, seed_history
from tests.querycount import count_queries

API = "/api/v1"


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"cp-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_the_list_does_not_grow_with_history(db_session: AsyncSession) -> None:
    small = await seed_history(db_session, (await _learner(db_session)).id, SMALL)
    large = await seed_history(db_session, (await _learner(db_session)).id, SMALL.scaled(12))
    with count_queries(db_session) as s:
        await chat_svc.list_conversations(db_session, small.learner_id, limit=5)
    with count_queries(db_session) as big:
        await chat_svc.list_conversations(db_session, large.learner_id, limit=5)
    assert len(big) <= len(s) + 2, big
    assert big.rows <= s.rows + 2, big


async def test_pages_walk_back_without_gaps_or_repeats(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    h = await seed_history(db_session, learner.id, SMALL.scaled(3))  # 15 + practice
    seen: list[uuid.UUID] = []
    before = None
    while True:
        page, has_more = await chat_svc.list_conversations(
            db_session, learner.id, limit=4, before=before
        )
        seen += [c.id for c in page]
        if not has_more:
            break
        before = page[-1].id
    assert len(seen) == len(set(seen)) == 16
    assert h.conversation_id in seen


async def test_ties_at_a_page_boundary_are_neither_skipped_nor_repeated(
    db_session: AsyncSession,
) -> None:
    """``created_at`` is the transaction's clock: conversations made together share it."""
    learner = await _learner(db_session)
    stamp = datetime.now(UTC).replace(tzinfo=None)
    for _ in range(5):
        db_session.add(Conversation(learner_id=learner.id, created_at=stamp))
    await db_session.flush()
    first, more = await chat_svc.list_conversations(db_session, learner.id, limit=3)
    second, done = await chat_svc.list_conversations(
        db_session, learner.id, limit=3, before=first[-1].id
    )
    assert more and not done
    assert len({c.id for c in first + second}) == 5


async def test_a_foreign_cursor_yields_the_first_page(db_session: AsyncSession) -> None:
    mine = await _learner(db_session)
    theirs = await _learner(db_session)
    await seed_history(db_session, mine.id, SMALL)
    other = Conversation(learner_id=theirs.id)
    db_session.add(other)
    await db_session.flush()
    page, _ = await chat_svc.list_conversations(db_session, mine.id, limit=3, before=other.id)
    first, _ = await chat_svc.list_conversations(db_session, mine.id, limit=3)
    assert [c.id for c in page] == [c.id for c in first]
    assert all(c.learner_id == mine.id for c in page)


async def test_the_endpoint_returns_a_bounded_page(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    await seed_history(db_session, api_learner.id, SMALL.scaled(12))  # 60 + practice
    body = (await api_client.get(f"{API}/conversations")).json()
    assert len(body["conversations"]) == 50
    assert body["has_more"] is True
    clamped = (await api_client.get(f"{API}/conversations", params={"limit": 10_000})).json()
    assert len(clamped["conversations"]) == 61  # under the 200 ceiling: everything
    assert clamped["has_more"] is False


async def test_a_conversation_is_readable_by_id(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The chat page used to find its conversation in the list; an old one is off the page."""
    h = await seed_history(db_session, api_learner.id, SMALL.scaled(12))
    oldest = (
        await chat_svc.list_conversations(db_session, api_learner.id, limit=200)
    )[0][-1]
    r = await api_client.get(f"{API}/conversations/{oldest.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(oldest.id)
    assert (await api_client.get(f"{API}/conversations/{uuid.uuid4()}")).status_code == 404
    assert h.learner_id == api_learner.id
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_conversation_page.py -q`
Expected: FAIL — `TypeError: list_conversations() got an unexpected keyword argument 'limit'`; the endpoint tests fail on `TypeError: list indices must be integers` and a 405 for the by-id GET.

- [ ] **Step 3: Implement**

`app/core/config.py`, after `chat_transcript_page_max`:

```python
    # The conversation list a client renders (S62): a page, like the transcript.
    chat_conversation_page_size: int = 50
    chat_conversation_page_max: int = 200
```

`app/services/chat.py` — replace `list_conversations`:

```python
async def list_conversations(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    archived: bool = False,
    limit: int,
    before: uuid.UUID | None = None,
) -> tuple[list[Conversation], bool]:
    """One page, newest first; archived conversations only when ``archived`` (S61, S62).

    Returns ``(conversations, has_more)``, keyed on ``(created_at, id)`` for the same reason as
    :func:`list_messages`: conversations made in one transaction share a timestamp. A stale or
    foreign cursor yields the first page.
    """
    stmt = select(Conversation).where(
        Conversation.learner_id == learner_id,
        Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None),
    )
    if before is not None:
        anchor = await session.get(Conversation, before)
        if anchor is not None and anchor.learner_id == learner_id:
            stmt = stmt.where(
                tuple_(Conversation.created_at, Conversation.id) < (anchor.created_at, anchor.id)
            )
    rows = list(
        (
            await session.scalars(
                stmt.order_by(Conversation.created_at.desc(), Conversation.id.desc())
                .limit(limit + 1)
                .options(selectinload(Conversation.conversation_sources))
            )
        ).all()
    )
    return rows[:limit], len(rows) > limit
```

`app/api/v1/chat.py` — replace the list endpoint and add the read:

```python
class ConversationPage(BaseModel):
    """One page of the learner's conversations, newest first (S62)."""

    conversations: list[ConversationRead]
    has_more: bool


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    session: SessionDep,
    settings: SettingsDep,
    learner: CurrentLearner,
    archived: Annotated[bool, Query()] = False,
    limit: int | None = None,
    before: uuid.UUID | None = None,
):
    """A page of conversations; ``before`` walks back. Bounded by default, clamped not refused."""
    size = limit if limit is not None else settings.chat_conversation_page_size
    size = max(1, min(size, settings.chat_conversation_page_max))
    rows, has_more = await svc.list_conversations(
        session, learner.id, archived=archived, limit=size, before=before
    )
    return ConversationPage(
        conversations=[ConversationRead.model_validate(c) for c in rows], has_more=has_more
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def read_conversation(conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """One conversation, archived or not — what the chat page opens, whatever page it is on."""
    conversation = await svc.get_conversation(session, conversation_id, learner_id=learner.id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conversation
```

Update the listed existing tests: wherever a test reads `GET /conversations` as a list, read `["conversations"]` from the JSON body (e.g. `(await api_client.get(f"{API}/conversations")).json()` → `(await api_client.get(f"{API}/conversations")).json()["conversations"]`; `r.json()` → `r.json()["conversations"]`). Tests that only check a status code stay as they are.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_conversation_page.py tests/test_authorization.py tests/test_chat.py tests/test_conversation_phase.py tests/test_practice_pause.py tests/test_removal.py tests/test_workflow.py tests/test_auth.py tests/test_admin_sudo.py tests/test_impersonation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py app/services/chat.py app/api/v1/chat.py tests/test_conversation_page.py tests/test_authorization.py tests/test_chat.py tests/test_conversation_phase.py tests/test_practice_pause.py tests/test_removal.py tests/test_workflow.py
git commit -m "feat(chat): the conversation list is a page, and a conversation reads by id [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: The source list pages

**Files:**
- Modify: `app/core/config.py` (two settings)
- Create: `app/services/sources.py` (`list_sources`)
- Modify: `app/api/v1/sources.py:179-199` (`SourcePage`, endpoint)
- Modify tests reading the list as an array: `tests/test_authorization.py:163`, `tests/test_removal.py:194-195`, `tests/test_sources_api.py:113,135`, `tests/test_visibility_sweep.py:726-729`
- Create: `tests/test_source_page.py`

**Interfaces:**
- Consumes: Task 1's `seed_history`, `count_queries`.
- Produces: `async sources.list_sources(session, learner_id, *, subject_id: uuid.UUID | None = None, archived: bool = False, limit: int, before: uuid.UUID | None = None) -> tuple[list[Source], bool]`; `SourcePage(sources: list[SourceRead], has_more: bool)`; `GET /api/v1/sources?subject_id&archived&limit&before` → `SourcePage`.

- [ ] **Step 1: Write the failing tests**

`tests/test_source_page.py`:

```python
"""The source list is a page, not the whole library (S62)."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.models.source import Source
from app.services import sources as sources_svc
from tests.history import SMALL, seed_history
from tests.querycount import count_queries

API = "/api/v1"


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"sp-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_the_list_does_not_grow_with_history(db_session: AsyncSession) -> None:
    small = await seed_history(db_session, (await _learner(db_session)).id, SMALL)
    large = await seed_history(db_session, (await _learner(db_session)).id, SMALL.scaled(12))
    with count_queries(db_session) as s:
        await sources_svc.list_sources(db_session, small.learner_id, limit=3)
    with count_queries(db_session) as big:
        await sources_svc.list_sources(db_session, large.learner_id, limit=3)
    assert len(big) <= len(s) + 2, big
    assert big.rows <= s.rows + 2, big


async def test_ties_at_a_page_boundary_are_neither_skipped_nor_repeated(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    stamp = datetime.now(UTC).replace(tzinfo=None)
    for i in range(5):
        db_session.add(
            Source(
                learner_id=learner.id,
                kind="file",
                origin=f"f{i}.txt",
                status="done",
                meta={},
                attempts=0,
                created_at=stamp,
            )
        )
    await db_session.flush()
    first, more = await sources_svc.list_sources(db_session, learner.id, limit=3)
    second, done = await sources_svc.list_sources(
        db_session, learner.id, limit=3, before=first[-1].id
    )
    assert more and not done
    assert len({s.id for s in first + second}) == 5


async def test_a_foreign_cursor_yields_the_first_page(db_session: AsyncSession) -> None:
    mine = await _learner(db_session)
    theirs = await _learner(db_session)
    await seed_history(db_session, mine.id, SMALL)
    foreign = await seed_history(db_session, theirs.id, SMALL)
    their_source = (await sources_svc.list_sources(db_session, foreign.learner_id, limit=1))[0][0]
    page, _ = await sources_svc.list_sources(db_session, mine.id, limit=2, before=their_source.id)
    first, _ = await sources_svc.list_sources(db_session, mine.id, limit=2)
    assert [s.id for s in page] == [s.id for s in first]


async def test_the_endpoint_returns_a_bounded_page(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    await seed_history(db_session, api_learner.id, SMALL.scaled(30))  # 120 sources
    body = (await api_client.get(f"{API}/sources")).json()
    assert len(body["sources"]) == 100
    assert body["has_more"] is True
    rest = (
        await api_client.get(f"{API}/sources", params={"before": body["sources"][-1]["id"]})
    ).json()
    assert len(rest["sources"]) == 20
    assert rest["has_more"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_source_page.py -q`
Expected: FAIL at collection — `ImportError: cannot import name 'sources' from 'app.services'`.

- [ ] **Step 3: Implement**

`app/core/config.py`, beside the conversation page settings:

```python
    # The source library a client renders (S62). Larger than the conversation page: the
    # picker shows many at once.
    sources_page_size: int = 100
    sources_page_max: int = 500
```

`app/services/sources.py`:

```python
"""Reading a learner's source library, a page at a time (S62).

The list used to return every source the learner had ever uploaded, in one query whose rows
grew with their library. It is a page now, keyed like the transcript and the conversation list
on ``(created_at, id)`` so uploads made in one transaction are neither skipped nor repeated.
"""

import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source import Source


async def list_sources(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    subject_id: uuid.UUID | None = None,
    archived: bool = False,
    limit: int,
    before: uuid.UUID | None = None,
) -> tuple[list[Source], bool]:
    """One page, newest first; ``(sources, has_more)``. A stale or foreign cursor yields the
    first page."""
    stmt = select(Source).where(
        Source.learner_id == learner_id,
        Source.archived_at.is_not(None) if archived else Source.archived_at.is_(None),
    )
    if subject_id is not None:
        stmt = stmt.where(Source.subject_id == subject_id)
    if before is not None:
        anchor = await session.get(Source, before)
        if anchor is not None and anchor.learner_id == learner_id:
            stmt = stmt.where(tuple_(Source.created_at, Source.id) < (anchor.created_at, anchor.id))
    rows = list(
        (
            await session.scalars(
                stmt.order_by(Source.created_at.desc(), Source.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    return rows[:limit], len(rows) > limit
```

`app/api/v1/sources.py` — add `SourcePage` and replace the endpoint (import `SettingsDep` from `app.api.deps` if not imported, and `from app.services import sources as sources_svc`):

```python
class SourcePage(BaseModel):
    """One page of the learner's sources, newest first (S62)."""

    sources: list[SourceRead]
    has_more: bool


@router.get("/sources", response_model=SourcePage)
async def list_sources(
    session: SessionDep,
    settings: SettingsDep,
    learner: CurrentLearner,
    subject_id: Annotated[uuid.UUID | None, Query()] = None,
    archived: Annotated[bool, Query()] = False,
    limit: int | None = None,
    before: uuid.UUID | None = None,
):
    """A page of the learner's sources, optionally scoped to a subject — backs the source
    picker and the Uploads page. Archived sources only with ``archived=true`` (S61)."""
    if subject_id is not None:
        await knowledge.require_visible_subject(session, subject_id, learner.id)
    size = limit if limit is not None else settings.sources_page_size
    size = max(1, min(size, settings.sources_page_max))
    rows, has_more = await sources_svc.list_sources(
        session, learner.id, subject_id=subject_id, archived=archived, limit=size, before=before
    )
    return SourcePage(sources=[SourceRead.model_validate(s) for s in rows], has_more=has_more)
```

Update the listed existing tests to read `["sources"]` from the JSON body where they treat it as a list. In `tests/test_visibility_sweep.py:726-729`, if the sweep compares raw bodies, compare `body["sources"]` and record a ruling.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_source_page.py tests/test_authorization.py tests/test_removal.py tests/test_sources_api.py tests/test_visibility_sweep.py tests/test_auth.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py app/services/sources.py app/api/v1/sources.py tests/test_source_page.py tests/test_authorization.py tests/test_removal.py tests/test_sources_api.py tests/test_visibility_sweep.py
git commit -m "feat(sources): the source list is a page [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: The frontend reads pages

**Files:**
- Modify: `frontend/src/api/schema.d.ts` (regenerated: `uv run poe api-types`)
- Modify: `frontend/src/api/hooks.ts` (`useConversations`, `useArchivedConversations`, `useSources`, `useAllSources`, `useArchivedSources`; new `useConversation`)
- Modify: `frontend/src/hooks/useChatConversation.ts:33-62`, `frontend/src/hooks/useChatConversation.test.tsx:31-40`
- Modify: `frontend/src/components/chat/ConversationSidebar.tsx`, `frontend/src/pages/Uploads.tsx`

**Interfaces:**
- Consumes: Tasks 2 and 3's endpoints.
- Produces: list hooks whose `data` stays a flat array (via `select`), plus `hasNextPage`/`fetchNextPage`; `useConversation(id)`.

- [ ] **Step 1: Regenerate types and watch the build fail**

Run: `uv run poe api-types && (cd frontend && npm run build)`
Expected: FAIL — type errors where list hooks return `ConversationPage`/`SourcePage` but consumers expect arrays.

- [ ] **Step 2: Implement the hooks**

In `frontend/src/api/hooks.ts`, replace the five list hooks with infinite queries whose `select` flattens, so every consumer keeps receiving an array:

```ts
type ConversationPage = { conversations: { id: string; created_at: string }[]; has_more: boolean };

function conversationPages(archived: boolean) {
  return {
    queryKey: archived ? ["conversations", "archived"] : ["conversations"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }: { pageParam: string | undefined }) => {
      const { data, error } = await api.GET("/api/v1/conversations", {
        params: { query: { ...(archived ? { archived: true } : {}), ...(pageParam ? { before: pageParam } : {}) } },
      });
      if (error) throw error;
      return data;
    },
    // The oldest conversation we hold is the next cursor; `has_more` is the server's answer.
    getNextPageParam: (last: ConversationPage) =>
      last.has_more ? last.conversations[last.conversations.length - 1]?.id : undefined,
  };
}

/** Newest-first pages (app/services/chat.py::list_conversations), flattened for consumers. */
export function useConversations() {
  return useInfiniteQuery({
    ...conversationPages(false),
    select: (d) => d.pages.flatMap((p) => p.conversations),
  });
}

export function useArchivedConversations() {
  return useInfiniteQuery({
    ...conversationPages(true),
    select: (d) => d.pages.flatMap((p) => p.conversations),
  });
}

/** One conversation by id — the chat page's source of truth, wherever it sits in the list. */
export function useConversation(conversationId: string | undefined) {
  return useQuery({
    queryKey: ["conversation", conversationId],
    enabled: conversationId !== undefined,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/conversations/{conversation_id}", {
        params: { path: { conversation_id: conversationId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}
```

Sources, the same way (keep `stillIngesting` and the polling):

```ts
function sourcePages(key: unknown[], query: { subject_id?: string; archived?: boolean }) {
  return {
    queryKey: key,
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }: { pageParam: string | undefined }) => {
      const { data, error } = await api.GET("/api/v1/sources", {
        params: { query: { ...query, ...(pageParam ? { before: pageParam } : {}) } },
      });
      if (error) throw error;
      return data;
    },
    getNextPageParam: (last: { sources: { id: string }[]; has_more: boolean }) =>
      last.has_more ? last.sources[last.sources.length - 1]?.id : undefined,
  };
}

export function useSources(subjectId: string | undefined) {
  return useInfiniteQuery({
    ...sourcePages(["sources", subjectId], { subject_id: subjectId }),
    enabled: subjectId !== undefined,
    select: (d) => d.pages.flatMap((p) => p.sources),
  });
}

export function useAllSources(subjectId: string | undefined) {
  return useInfiniteQuery({
    ...sourcePages(["sources", "all", subjectId], { subject_id: subjectId }),
    select: (d) => d.pages.flatMap((p) => p.sources),
    // Every loaded page is refetched; polling stops once nothing loaded is in flight.
    refetchInterval: (query) =>
      stillIngesting(query.state.data?.pages.flatMap((p) => p.sources)) ? INGESTION_POLL_MS : false,
  });
}

export function useArchivedSources() {
  return useInfiniteQuery({
    ...sourcePages(["sources", "archived"], { archived: true }),
    select: (d) => d.pages.flatMap((p) => p.sources),
  });
}
```

Keep the existing doc comments on `useSources`/`useAllSources` above their new bodies. If `openapi-fetch` rejects the spread query types, type the `query` object with the generated `paths["/api/v1/sources"]["get"]["parameters"]["query"]` and record a ruling.

- [ ] **Step 3: The chat page reads its conversation by id; the lists load more**

`frontend/src/hooks/useChatConversation.ts`: replace

```ts
  const conversationsQuery = useConversations();
  // An archived conversation is not in the main list but still opens, read-only (S61).
  const archivedQuery = useArchivedConversations();
```

with

```ts
  // Read by id, not found in the list: the list is a page now (S62), and the conversation
  // being opened can be older than any page loaded — or archived, which still opens (S61).
  const conversationQuery = useConversation(conversationId);
```

and

```ts
  const conversation = [...(conversationsQuery.data ?? []), ...(archivedQuery.data ?? [])].find(
    (c) => c.id === conversationId,
  );
```

with `const conversation = conversationQuery.data;`. Replace every other `conversationsQuery`/`archivedQuery` reference in the file with `conversationQuery` (an `isLoading` check becomes `conversationQuery.isLoading`), fix the imports, and where a mutation invalidates `["conversations"]` also invalidate `["conversation", conversationId]`.

`frontend/src/hooks/useChatConversation.test.tsx`: in the `vi.mock("../api/hooks", …)` factory, add `useConversation: () => ({ data: undefined, isLoading: false }),` beside `useConversations`.

`frontend/src/components/chat/ConversationSidebar.tsx`: take `hasNextPage`, `fetchNextPage`, `isFetchingNextPage` from `useConversations()` and render after the conversation rows:

```tsx
        {hasNextPage && (
          <button
            className="btn btn-ghost btn-xs mt-1 w-full"
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        )}
```

`frontend/src/pages/Uploads.tsx`: the same button under `<SourceList …/>`, from `useAllSources(...)`.

- [ ] **Step 4: Run the frontend gates**

Run: `cd frontend && npm run build && npm test -- --run && npm run lint`
Expected: build succeeds; vitest all pass; lint clean.

Run: `uv run poe api-contract`
Expected: no diff.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/schema.d.ts frontend/src/api/hooks.ts frontend/src/hooks/useChatConversation.ts frontend/src/hooks/useChatConversation.test.tsx frontend/src/components/chat/ConversationSidebar.tsx frontend/src/pages/Uploads.tsx
git commit -m "feat(frontend): conversations and sources load a page at a time; the chat page reads its conversation by id [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: Every hot path has a history budget

**Files:**
- Create: `tests/perf/__init__.py` (empty), `tests/perf/paths.py`, `tests/test_history_budgets.py`

**Interfaces:**
- Consumes: Task 1's `seed_history`, `SMALL`, `SeededHistory`, `count_queries`; Task 2's `chat.list_conversations(..., limit=)`; Task 3's `sources.list_sources(..., limit=)`.
- Produces: `PATHS: dict[str, Callable[[AsyncSession, SeededHistory], Awaitable[object]]]` with keys `turn`, `practice`, `conversation_list`, `transcript`, `source_list`, `reviews_due`, `activity`, `subject_mastery`, `memory_list`, `lesson_plan`, `plan_revision`, `notes_index`, `profile_refresh`.

- [ ] **Step 1: Write the paths and the failing budgets**

`tests/perf/paths.py`:

```python
"""Each hot path once, as the call its endpoint makes (S62).

The budget tests and ``poe perf-report`` run exactly these, so a budget and a timing always
describe the same work. Model calls go to the fake client; what is measured is ours.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.services import analytics, lesson_plan, memory, notes, profile, session_runner, workflow
from app.services import chat as chat_svc
from app.services import sources as sources_svc
from tests.history import SeededHistory

PRESENT = "Here's a worked example. Now try: explain it in your own words."
RIGHT_GRADE = '{"score": 0.9, "rationale": "good"}'
RESPOND = "Great job, you've got it!"


async def turn(session: AsyncSession, h: SeededHistory) -> object:
    settings = get_settings()
    conversation = await chat_svc.get_conversation(session, h.conversation_id, learner_id=h.learner_id)
    assert conversation is not None
    history = await chat_svc.recent_messages(
        session, h.conversation_id, limit=settings.chat_history_max_messages
    )
    return [
        e
        async for e in chat_svc.run_tutor_turn(
            session,
            fake_llm_client(),
            learner_id=h.learner_id,
            conversation=conversation,
            history=history,
            user_content="Can you explain that again?",
            max_tokens=settings.chat_max_tokens,
        )
    ]


async def practice(session: AsyncSession, h: SeededHistory) -> object:
    """Pose a question and answer it: both halves of a practice round."""
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND)]
    )
    conversation = await session.get(Conversation, h.practice_conversation_id)
    assert conversation is not None
    events = []
    for content, resume in (("let's practice", False), ("my answer", True)):
        events += [
            e
            async for e in workflow.run_workflow_turn(
                session,
                llm,
                learner_id=h.learner_id,
                conversation=conversation,
                user_content=content,
                max_tokens=300,
                max_rounds=3,
                resume=resume,
            )
        ]
    return events


async def conversation_list(session: AsyncSession, h: SeededHistory) -> object:
    return await chat_svc.list_conversations(
        session, h.learner_id, limit=get_settings().chat_conversation_page_size
    )


async def transcript(session: AsyncSession, h: SeededHistory) -> object:
    size = get_settings().chat_transcript_page_size
    page, has_more = await chat_svc.list_messages(session, h.conversation_id, limit=size)
    if has_more and page:
        await chat_svc.list_messages(session, h.conversation_id, limit=size, before=page[0].id)
    return page


async def source_list(session: AsyncSession, h: SeededHistory) -> object:
    return await sources_svc.list_sources(
        session, h.learner_id, limit=get_settings().sources_page_size
    )


async def reviews_due(session: AsyncSession, h: SeededHistory) -> object:
    return await session_runner.due_review_items(
        session,
        fake_llm_client(),
        learner_id=h.learner_id,
        item_limit=get_settings().reviews_due_item_limit,
    )


async def activity(session: AsyncSession, h: SeededHistory) -> object:
    return await analytics.get_activity(session, h.learner_id)


async def subject_mastery(session: AsyncSession, h: SeededHistory) -> object:
    return await analytics.subject_mastery(session, h.learner_id, h.subject_id)


async def memory_list(session: AsyncSession, h: SeededHistory) -> object:
    return await memory.list_memories(session, h.learner_id)


async def lesson_plan_read(session: AsyncSession, h: SeededHistory) -> object:
    return await lesson_plan.get_lesson_plan(session, h.learner_id, h.subject_id)


async def plan_revision(session: AsyncSession, h: SeededHistory) -> object:
    return await lesson_plan.revise_plan(session, learner_id=h.learner_id, subject_id=h.subject_id)


async def notes_index(session: AsyncSession, h: SeededHistory) -> object:
    return await notes.notes_index(session, h.learner_id, h.subject_id)


async def profile_refresh(session: AsyncSession, h: SeededHistory) -> object:
    return await profile.refresh_profile(session, h.learner_id, fake_llm_client(), force=True)


PATHS: dict[str, Callable[[AsyncSession, SeededHistory], Awaitable[object]]] = {
    "turn": turn,
    "practice": practice,
    "conversation_list": conversation_list,
    "transcript": transcript,
    "source_list": source_list,
    "reviews_due": reviews_due,
    "activity": activity,
    "subject_mastery": subject_mastery,
    "memory_list": memory_list,
    "lesson_plan": lesson_plan_read,
    "plan_revision": plan_revision,
    "notes_index": notes_index,
    "profile_refresh": profile_refresh,
}
```

`tests/test_history_budgets.py`:

```python
"""No hot path costs more because the learner has been here longer (S62).

Each path runs for a learner with a small history and for one with four times as much, on the
same graph. Statements and rows must not grow; the slack of two absorbs a conditional branch,
not growth. Paths the first run found growing are marked for S62 part B with the measured
cause, and the marker is strict: fixing one without removing it fails the suite.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from tests.history import SMALL, seed_history
from tests.perf.paths import PATHS
from tests.querycount import count_queries

PART_B: dict[str, str] = {}
"""Path → measured cause, for paths that grow and are fixed in S62 part B."""


async def _seeded(session: AsyncSession, scale: int):
    learner = Learner(handle=f"hb-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return await seed_history(session, learner.id, SMALL.scaled(scale))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, marks=pytest.mark.xfail(strict=True, reason=PART_B[n]))
        if n in PART_B
        else n
        for n in sorted(PATHS)
    ],
)
async def test_a_hot_path_does_not_grow_with_history(name: str, db_session: AsyncSession) -> None:
    small = await _seeded(db_session, 1)
    large = await _seeded(db_session, 4)
    path = PATHS[name]

    with count_queries(db_session) as s:
        await path(db_session, small)
    with count_queries(db_session) as big:
        await path(db_session, large)

    assert len(big) <= len(s) + 2, f"{name}: statements grew\nsmall {s!r}\nlarge {big!r}"
    assert big.rows <= s.rows + 2, f"{name}: rows grew\nsmall {s!r}\nlarge {big!r}"
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_history_budgets.py -q`
Expected: `conversation_list` and `source_list` PASS (Tasks 2–3). Any other path that FAILS is a measured growth: for each, read the printed statements, write one line naming the cause (e.g. "reviews_due: one row per due component — the due list has no limit"), and add `"<path>": "<cause>"` to `PART_B`. A path that errors (not a budget failure) is a harness defect: fix the path function and ledger a ruling.

- [ ] **Step 3: Run again**

Run: `uv run pytest tests/test_history_budgets.py -q`
Expected: every path passes or is an expected xfail; no XPASS, no error. Total time under 10 s (`--durations=5` to check); if over, lower `SMALL` shares that the slowest path does not read and ledger a ruling.

- [ ] **Step 4: Commit**

```bash
git add tests/perf/__init__.py tests/perf/paths.py tests/test_history_budgets.py
git commit -m "test(perf): every hot path has a history budget; measured growth is named for part B [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 6: `poe perf-report`, and its first run

**Files:**
- Create: `tests/perf/report.py`, `tests/test_perf_report.py`
- Modify: `pyproject.toml` (`perf-report` task)
- Modify: `docs/superpowers/specs/2026-10-09-long-history-performance-design.md` ("Results")

**Interfaces:**
- Consumes: Task 1's `seed_history`, `POWER_USER`, `ORDINARY`, `SeededHistory`; Task 5's `PATHS`.
- Produces: `PathResult(name, p50_ms, p95_ms, max_ms, statements, rows, slowest: list[tuple[float, str]])`; `async measure(engine, history, paths, *, warmup, runs) -> list[PathResult]`; `render(results, *, budget_ms=250.0) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_perf_report.py`:

```python
"""The perf report measures each path and marks the ones over budget (S62)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.learner import Learner
from tests.history import SMALL, seed_history
from tests.perf.paths import PATHS
from tests.perf.report import PathResult, measure, render


def test_render_marks_paths_over_budget() -> None:
    fast = PathResult("memory_list", 2.0, 3.0, 4.0, 1, 30, [(1.5, "SELECT memories")])
    slow = PathResult("turn", 180.0, 310.0, 400.0, 22, 900, [(120.0, "SELECT chunks")])
    table = render([fast, slow], budget_ms=250.0)
    assert "| turn |" in table and "over" in table.split("| turn |")[1].split("\n")[0]
    assert "over" not in table.split("| memory_list |")[1].split("\n")[0]
    assert "SELECT chunks" in table


async def test_measure_times_each_path(engine: AsyncEngine) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        learner = Learner(handle=f"pr-{uuid.uuid4().hex[:8]}")
        session.add(learner)
        await session.commit()
        history = await seed_history(session, learner.id, SMALL)
    try:
        results = await measure(
            engine, history, {"memory_list": PATHS["memory_list"]}, warmup=1, runs=3
        )
        (r,) = results
        assert r.name == "memory_list" and r.p50_ms > 0 and r.statements >= 1 and r.rows >= 1
        assert r.p50_ms <= r.p95_ms <= r.max_ms
    finally:
        async with AsyncSession(engine) as session:
            from app.services import retention
            from app.storage.memory import InMemoryBlobStore

            await retention.delete_learner(session, InMemoryBlobStore(), history.learner_id)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_perf_report.py -q`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'tests.perf.report'`.

- [ ] **Step 3: Implement**

`tests/perf/report.py`:

```python
"""``poe perf-report``: time every hot path against a long history (S62).

Seeds ``guru_perf`` — its own database, never dev or test data — with one power user (five
times a year of daily use) and twenty ordinary learners, so tables and indexes look like a
shared database rather than one person's. Then runs each path in ``tests.perf.paths`` in
process: warm-ups, then timed runs, with every statement timed. A report, not a gate: it always
exits 0, and a path over budget is marked for a person to read.

    uv run poe perf-report [--reseed] [--runs N] [--json PATH]
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

SEED_VERSION = 1
BUDGET_MS = 250.0


@dataclass
class PathResult:
    name: str
    p50_ms: float
    p95_ms: float
    max_ms: float
    statements: int
    rows: int
    slowest: list[tuple[float, str]] = field(default_factory=list)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


async def measure(
    engine: AsyncEngine,
    history: Any,
    paths: dict[str, Callable[[AsyncSession, Any], Awaitable[object]]],
    *,
    warmup: int,
    runs: int,
) -> list[PathResult]:
    results = []
    for name, path in paths.items():
        timings: list[float] = []
        statements: list[tuple[float, str, int]] = []
        sync_engine = engine.sync_engine

        def before(conn, cursor, statement, parameters, context, executemany):
            context._s62_start = time.perf_counter()

        def after(conn, cursor, statement, parameters, context, executemany):
            elapsed = (time.perf_counter() - context._s62_start) * 1000
            statements.append((elapsed, statement.split("\n")[0][:140], cursor.rowcount))

        for i in range(warmup + runs):
            counting = i >= warmup
            if counting:
                statements.clear() if i > warmup else None
                event.listen(sync_engine, "before_cursor_execute", before)
                event.listen(sync_engine, "after_cursor_execute", after)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                start = time.perf_counter()
                await path(session, history)
                await session.commit()
                elapsed = (time.perf_counter() - start) * 1000
            if counting:
                event.remove(sync_engine, "before_cursor_execute", before)
                event.remove(sync_engine, "after_cursor_execute", after)
                timings.append(elapsed)
        # `statements` holds the last timed run only: one run's statements and rows.
        results.append(
            PathResult(
                name=name,
                p50_ms=statistics.median(timings),
                p95_ms=_p95(timings),
                max_ms=max(timings),
                statements=len(statements),
                rows=sum(max(n, 0) for _, _, n in statements),
                slowest=[(ms, s) for ms, s, _ in sorted(statements, reverse=True)[:3]],
            )
        )
    return results


def render(results: list[PathResult], *, budget_ms: float = BUDGET_MS) -> str:
    lines = [
        "| path | p50 ms | p95 ms | max ms | statements | rows | budget |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in results:
        flag = f"over {budget_ms:.0f}" if r.p95_ms > budget_ms else "ok"
        lines.append(
            f"| {r.name} | {r.p50_ms:.1f} | {r.p95_ms:.1f} | {r.max_ms:.1f} | "
            f"{r.statements} | {r.rows} | {flag} |"
        )
    lines.append("")
    lines.append("Slowest statements (one timed run):")
    for r in results:
        lines.append(f"- {r.name}: " + "; ".join(f"{ms:.1f} ms `{s}`" for ms, s in r.slowest))
    return "\n".join(lines)


@asynccontextmanager
async def _discard_accounting() -> AsyncIterator[Any]:
    class _Discarded:
        def add(self, obj: object) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def execute(self, *args: object, **kwargs: object) -> None:
            pass

    yield _Discarded()


async def _seed(engine: AsyncEngine, *, reseed: bool) -> Any:
    from app.models.learner import Learner
    from tests.history import ORDINARY, POWER_USER, SeededHistory, seed_history

    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS perf_seed (version int PRIMARY KEY, payload jsonb)")
        )
        row = (
            await conn.execute(
                text("SELECT payload FROM perf_seed WHERE version = :v"), {"v": SEED_VERSION}
            )
        ).first()
    if row is not None and not reseed:
        payload = row[0]
        return SeededHistory(**{k: uuid.UUID(v) for k, v in payload.items()})
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(text("DELETE FROM learners WHERE handle LIKE 'perf-%'"))
        await session.execute(text("DELETE FROM perf_seed"))
        await session.commit()
        for i in range(20):
            other = Learner(handle=f"perf-ordinary-{i}")
            session.add(other)
            await session.commit()
            await seed_history(session, other.id, ORDINARY)
            print(f"seeded ordinary learner {i + 1}/20", file=sys.stderr)
        power = Learner(handle="perf-power-user")
        session.add(power)
        await session.commit()
        history = await seed_history(session, power.id, POWER_USER)
        await session.execute(
            text("INSERT INTO perf_seed (version, payload) VALUES (:v, CAST(:p AS jsonb))"),
            {"v": SEED_VERSION, "p": json.dumps({k: str(v) for k, v in asdict(history).items()})},
        )
        await session.commit()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("ANALYZE"))
    return history


async def _main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="poe perf-report")
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)

    from app.services.llm_log import set_accounting_session_factory
    from tests.perf.paths import PATHS

    set_accounting_session_factory(_discard_accounting)
    engine = create_async_engine(os.environ["GURU_DATABASE_URL"])
    try:
        history = await _seed(engine, reseed=args.reseed)
        results = await measure(engine, history, PATHS, warmup=3, runs=args.runs)
    finally:
        await engine.dispose()
    print(render(results))
    if args.json_path:
        with open(args.json_path, "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)


def main() -> None:
    from app.core.config import Settings
    from tests.testdb import test_database_url

    url = test_database_url(Settings().database_url, "_perf")
    subprocess.run([sys.executable, "-m", "tests.testdb", "--suffix", "_perf"], check=True)
    # Before anything reads settings: the app's own engine and caps must see this database.
    os.environ["GURU_DATABASE_URL"] = url
    asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
```

Fix the warm-up/clear logic while implementing if it reads awkwardly: the requirement is that `statements` holds exactly the last timed run's statements and `timings` holds every timed run — the test pins both.

`pyproject.toml`, after `extraction-report`:

```toml
perf-report = "python -m tests.perf.report"
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_perf_report.py -q`
Expected: PASS.

- [ ] **Step 5: Run the report for real**

Run (background; seeding takes minutes): `uv run poe perf-report --json .superpowers/sdd/2026-10-09-long-history-performance/perf-1.json > .superpowers/sdd/2026-10-09-long-history-performance/perf-1.md 2>&1`
Expected: exit 0; a table with 13 rows. If a path errors at this size, fix the harness (not the product) and ledger a ruling.

- [ ] **Step 6: Record the results**

Replace "Filled in by part A's first report run." in the spec's "Results" with: the date, the machine (`uname -m`, Postgres version from `SELECT version()`), the table from `perf-1.md`, the slowest statements, the list of `PART_B` entries from Task 5, and the paths the fix rule selects (growth, or p95 > 250 ms).

- [ ] **Step 7: Commit**

```bash
git add tests/perf/report.py tests/test_perf_report.py pyproject.toml docs/superpowers/specs/2026-10-09-long-history-performance-design.md
git commit -m "feat(perf): poe perf-report times every hot path against a 5x history; first results recorded [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 7: Gates and docs

**Files:**
- Modify: `docs/RUNBOOK.md` (new section "20. Performance against a long history (S62)")
- Modify: `docs/guru-suggestions-tracker.md` (S62 row: Part A done, results summary, part B list)

- [ ] **Step 1: Full gates**

Run (background): `uv run poe check > .superpowers/sdd/2026-10-09-long-history-performance/check.log 2>&1`, then `tail -3` of it.
Expected: all passed (plus any strict xfails from Task 5), 10 skipped.

Run: `uv run poe format-check && uv run poe api-contract && (cd frontend && npm run build && npm test -- --run && npm run lint)`
Expected: all clean.

- [ ] **Step 2: RUNBOOK**

Append:

```markdown
## 20. Performance against a long history (S62)

`uv run poe perf-report` seeds `guru_perf` (its own database) with a power user — five times a
year of daily use — and twenty ordinary learners, then times every hot path in
`tests/perf/paths.py` in process: p50, p95, max, statements, rows, and each path's slowest
statements. A path whose p95 is over 250 ms is marked. The seed is reused between runs;
`--reseed` rebuilds it, `--runs N` changes the sample, `--json PATH` saves results to compare.

It is a report, not a gate. The gate is `tests/test_history_budgets.py` in `poe check`: every
path's statements and rows at four times the history must not exceed the small history's plus
two. A new hot path belongs in `tests/perf/paths.py`, which puts it under both.

The conversation and source lists are pages (`limit`, `before`; 50/200 and 100/500); a
conversation opens by id (`GET /conversations/{id}`).
```

- [ ] **Step 3: Tracker**

Update the S62 row's "remaining" text to name part B (the paths the rule selected, from the spec's Results), or, if none, move S62 to Completed (Live v0 15 → 14, Done 50 → 51; next up S53). Note deferred minors from the final review there.

- [ ] **Step 4: Commit**

```bash
git add docs/RUNBOOK.md docs/guru-suggestions-tracker.md
git commit -m "docs: record long-history measurement [S62]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```
