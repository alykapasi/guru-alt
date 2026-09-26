# Source Scope and Honest Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every path that grounds generation in a learner's sources uses one scope rule and one grounding policy, with per-subject untagged-source and sources-only switches and a server-derived coverage label on replies.

**Architecture:** A new `app/rag/scope.py` resolves `(learner, subject, picked sources)` into a `SourceScope` (or `None` for a General conversation) and `retrieve()` takes that scope. A new `app/services/grounding.py` owns the prompt instructions (normal / sources-only × passages / nothing), and a pure `app/rag/coverage.py` derives the coverage label from stored facts. Lessons, chat, practice, agent search and onboarding all call these.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, pytest; React + TypeScript + Vite, vitest.

**Spec:** `docs/superpowers/specs/2026-09-26-source-scope-and-support-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract`.
- Frontend changes also green on `cd frontend && npm run build` and `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`.
- One tracker id per commit subject: `[S26]` or `[S28]` as each task states.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; run `git status` after staging.
- Never reset, amend, rebase, squash or force-push. Do not push. Do not open a PR.
- Never print `.env` or any secret value. Never set `GURU_JEV_SMOKE`.
- No backfill: existing subjects get both switches off; existing messages get `grounding_count` NULL.

## Review Focus

1. A subject whose settings change after lessons were cached — the next generation must not reuse a block written under the old policy (Task 4 test pins it via the cache key).
2. A learner in a General chat asking the agent to "search my notes" — must get the "no materials in scope" answer, not their whole library (Task 2 test).
3. A conversation with picked sources in a subject that has untagged sources switched on — picked sources win; untagged ones stay out (Task 2 test).
4. Messages written before this change — `grounding_count` NULL → no coverage label, never "Not from your materials" (Task 5 and Task 6 tests).
5. `PATCH …/source-settings` with an empty body — a no-op that returns the subject unchanged, not a 422 (Task 1 test).

## File Map

| File | Responsibility |
|---|---|
| `db/migrations/versions/0063_source_scope_settings.py` (create) | two subject switches + `messages.grounding_count` |
| `app/models/knowledge.py`, `app/models/chat.py` (modify) | ORM columns |
| `app/schemas/knowledge.py` (modify) | `SubjectRead` fields, `SourceSettingsUpdate` |
| `app/api/v1/knowledge.py` (modify) | `PATCH /subjects/{id}/source-settings` |
| `app/rag/scope.py` (create) | `SourceScope`, `resolve_scope` |
| `app/rag/retrieval.py` (modify) | `retrieve(..., scope=...)` |
| `app/rag/coverage.py` (create) | `Coverage`, `coverage()`, `cited_source_count()` |
| `app/services/grounding.py` (create) | instructions, `format_grounding`, `policy_note` |
| `app/services/turn_common.py` (modify) | drop moved grounding helpers; `add_message(grounding_count=)` |
| `app/services/{content,chat,workflow,agentic,onboarding}.py`, `app/agent/tools.py`, `app/api/v1/{sources,content}.py` (modify) | use scope + policy |
| `app/schemas/{chat,content}.py` (modify) | `grounding_count`, computed `coverage`, `cited_source_count` |
| `frontend/src/components/chat/CoverageChip.tsx` (+test) (create) | the label |
| `frontend/src/components/lessons/SourceSettingsPanel.tsx` (+test) (create) | the two switches |
| `frontend/src/api/hooks.ts`, `MessageBlock.tsx`, `MessageList.tsx`, `Lessons.tsx`, `UploadForm.tsx`, `api/schema.d.ts` (modify) | wiring |
| `docs/guru-suggestions-tracker.md` (modify) | S26, S28, S80 rows |

---

### Task 1: Schema, model columns and the settings endpoint [S26]

**Files:**
- Create: `db/migrations/versions/0063_source_scope_settings.py`
- Modify: `app/models/knowledge.py` (class `Subject`), `app/models/chat.py` (class `Message`), `app/schemas/knowledge.py`, `app/api/v1/knowledge.py`
- Test: `tests/test_source_settings.py` (create)

**Interfaces:**
- Produces: `Subject.include_untagged_sources: bool`, `Subject.sources_only: bool`, `Message.grounding_count: int | None`; `SubjectRead.include_untagged_sources`, `SubjectRead.sources_only`; `PATCH /api/v1/subjects/{subject_id}/source-settings` with body `SourceSettingsUpdate` returning `SubjectRead`.

- [ ] **Step 1: Write the failing tests** — `tests/test_source_settings.py`:

```python
"""Per-subject source switches (S26): who may change them, and what an empty change does."""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject
from app.models.learner import Learner

API = "/api/v1"


async def _subject(session: AsyncSession, owner: uuid.UUID | None) -> Subject:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Calculus", owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    return subject


async def test_a_new_subject_has_both_switches_off(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, api_learner.id)
    await db_session.commit()

    body = (await api_client.get(f"{API}/subjects/{subject.id}")).json()

    assert body["include_untagged_sources"] is False
    assert body["sources_only"] is False


async def test_the_owner_can_change_either_switch_alone(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, api_learner.id)
    await db_session.commit()

    first = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"sources_only": True}
    )
    second = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"include_untagged_sources": True}
    )

    assert first.status_code == 200
    assert first.json()["sources_only"] is True
    assert first.json()["include_untagged_sources"] is False
    assert second.json()["sources_only"] is True, "a field left out is left unchanged"
    assert second.json()["include_untagged_sources"] is True


async def test_an_empty_change_returns_the_subject_unchanged(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, api_learner.id)
    await db_session.commit()

    response = await api_client.patch(f"{API}/subjects/{subject.id}/source-settings", json={})

    assert response.status_code == 200
    assert response.json()["sources_only"] is False


async def test_a_curated_subject_refuses_the_change(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject = await _subject(db_session, None)
    await db_session.commit()

    response = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"sources_only": True}
    )

    assert response.status_code == 403


async def test_a_strangers_subject_is_not_found(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    stranger = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    subject = await _subject(db_session, stranger.id)
    await db_session.commit()

    response = await api_client.patch(
        f"{API}/subjects/{subject.id}/source-settings", json={"sources_only": True}
    )

    assert response.status_code == 404
    await db_session.refresh(subject)
    assert subject.sources_only is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_source_settings.py -v`
Expected: FAIL — `KeyError: 'include_untagged_sources'` / 404/405 on the PATCH route.

- [ ] **Step 3: Migration** — `db/migrations/versions/0063_source_scope_settings.py`:

```python
"""Per-subject source switches and per-message grounding counts (S26, S28).

Nothing to backfill. Both switches start off, which is the V05 default: untagged material is
not added to a subject until its owner says so. A message written before this has no count,
and NULL says exactly that — it is not zero, because nobody measured it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_source_scope_settings"
down_revision: str | Sequence[str] | None = "0062_decision_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subjects",
        sa.Column(
            "include_untagged_sources", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "subjects",
        sa.Column("sources_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("messages", sa.Column("grounding_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "grounding_count")
    op.drop_column("subjects", "sources_only")
    op.drop_column("subjects", "include_untagged_sources")
```

- [ ] **Step 4: ORM columns.** In `app/models/knowledge.py`, class `Subject`, after `private_source_derived`:

```python
    # What this subject may draw on (S26, V05). Untagged sources — most uploads, since the tag
    # is optional — are admitted only when the owner switches this on; nothing unassigned is
    # added to a subject silently.
    include_untagged_sources: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Teach only from the subject's sources: where they do not cover something, say so rather
    # than answering from general knowledge. Read by every grounding path (app.rag.scope).
    sources_only: Mapped[bool] = mapped_column(server_default=false(), default=False)
```

In `app/models/chat.py`, class `Message`, after `citations`:

```python
    # How many passages were offered to the model for this reply (S28). NULL means there was no
    # library scope — a General conversation, a refinement reply, or a row from before this was
    # recorded — which is different from 0, "searched and found nothing".
    grounding_count: Mapped[int | None] = mapped_column(default=None)
```

- [ ] **Step 5: Schemas.** In `app/schemas/knowledge.py`, add to `SubjectRead` after `private_source_derived`:

```python
    # The owner's source switches (S26); curated subjects always carry the defaults.
    include_untagged_sources: bool = False
    sources_only: bool = False
```

and add a new class after `SubjectRead`:

```python
class SourceSettingsUpdate(BaseModel):
    """A partial change to a subject's source switches; a field left out stays as it is."""

    include_untagged_sources: bool | None = None
    sources_only: bool | None = None
```

- [ ] **Step 6: Endpoint.** In `app/api/v1/knowledge.py`, import `SourceSettingsUpdate` in the schemas block, and add after `get_subject`:

```python
@router.patch("/subjects/{subject_id}/source-settings", response_model=SubjectRead)
async def update_source_settings(
    subject_id: uuid.UUID, data: SourceSettingsUpdate, session: SessionDep, learner: CurrentLearner
):
    """Change what this subject may draw on (S26). Owner only; curated subjects keep the
    defaults, so the same 403 as any other change to the shared library."""
    subject = await _visible_subject(session, subject_id, learner)
    _require_writable(subject, learner)
    if data.include_untagged_sources is not None:
        subject.include_untagged_sources = data.include_untagged_sources
    if data.sources_only is not None:
        subject.sources_only = data.sources_only
    await session.commit()
    await session.refresh(subject)
    return subject
```

- [ ] **Step 7: Run tests and the gate**

Run: `uv run pytest tests/test_source_settings.py -v` → PASS.
Run: `uv run poe api-types` (regenerates `frontend/src/api/schema.d.ts`), then `uv run poe check && uv run poe format-check && uv run poe api-contract` → all green.

- [ ] **Step 8: Commit**

```bash
git add db/migrations/versions/0063_source_scope_settings.py app/models/knowledge.py app/models/chat.py app/schemas/knowledge.py app/api/v1/knowledge.py tests/test_source_settings.py frontend/src/api/schema.d.ts
git status
git commit -m "feat(sources): per-subject untagged and sources-only switches [S26]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: One scope rule for every retrieval [S26]

**Files:**
- Create: `app/rag/scope.py`
- Modify: `app/rag/retrieval.py`, `app/services/content.py:111-128`, `app/services/chat.py:549-561`, `app/services/workflow.py:224-235`, `app/services/agentic.py:78-85`, `app/agent/tools.py` (`build_tools`, `_search_materials_tool`), `app/services/onboarding.py:150-165`, `app/api/v1/sources.py:175-195`, `tests/eval/harness.py:284`
- Modify tests (call-site migration): `tests/test_retrieval.py`, `tests/test_v0_web_policy.py`, `tests/test_agent_tools.py`, `tests/test_content.py`, `tests/test_citation_support.py`
- Test: `tests/test_scope.py` (create)

**Interfaces:**
- Consumes: `Subject.include_untagged_sources`, `Subject.sources_only` (Task 1).
- Produces:
  ```python
  @dataclass(frozen=True)
  class SourceScope:
      learner_id: uuid.UUID
      subject_id: uuid.UUID | None = None
      topic_id: uuid.UUID | None = None
      source_ids: tuple[uuid.UUID, ...] = ()
      include_untagged: bool = False
      sources_only: bool = False

  async def resolve_scope(session, *, learner_id, subject_id, source_ids=()) -> SourceScope | None
  async def retrieve(session, llm, query, *, scope: SourceScope, limit=10, candidates=50) -> list[RetrievalHit]
  def build_tools(session, llm, *, scope: SourceScope | None, citations: CitationAccumulator | None = None) -> list[Tool]
  NO_SCOPE_RESULT: str  # in app/agent/tools.py
  ```

- [ ] **Step 1: Write the failing scope tests** — `tests/test_scope.py`:

```python
"""One scope rule for every retrieval path (S26, V05)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import NO_SCOPE_RESULT, build_tools
from app.llm.registry import fake_llm_client
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.rag.scope import SourceScope, resolve_scope


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _subject(session: AsyncSession, owner: Learner, **switches: bool) -> Subject:
    subject = Subject(
        slug=f"s-{uuid.uuid4().hex[:8]}", name="S", owner_learner_id=owner.id, **switches
    )
    session.add(subject)
    await session.flush()
    return subject


async def test_a_general_conversation_has_no_scope(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    assert await resolve_scope(db_session, learner_id=learner.id, subject_id=None) is None


async def test_a_subject_leaves_untagged_sources_out_by_default(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _subject(db_session, learner)

    scope = await resolve_scope(db_session, learner_id=learner.id, subject_id=subject.id)

    assert scope == SourceScope(learner_id=learner.id, subject_id=subject.id)


async def test_a_subject_carries_both_switches(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _subject(
        db_session, learner, include_untagged_sources=True, sources_only=True
    )

    scope = await resolve_scope(db_session, learner_id=learner.id, subject_id=subject.id)

    assert scope is not None
    assert scope.include_untagged and scope.sources_only


async def test_picked_sources_override_the_untagged_switch(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _subject(db_session, learner, include_untagged_sources=True)
    picked = uuid.uuid4()

    scope = await resolve_scope(
        db_session, learner_id=learner.id, subject_id=subject.id, source_ids=[picked]
    )

    assert scope is not None
    assert scope.source_ids == (picked,)
    assert scope.include_untagged is False


async def test_sources_without_a_subject_scope_to_exactly_those(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    picked = uuid.uuid4()

    scope = await resolve_scope(
        db_session, learner_id=learner.id, subject_id=None, source_ids=[picked]
    )

    assert scope == SourceScope(learner_id=learner.id, source_ids=(picked,))


async def test_agent_search_in_a_general_chat_reaches_no_materials(
    db_session: AsyncSession,
) -> None:
    """The leak this closes: a chat labelled "no library grounding" searched every source."""
    tools = build_tools(db_session, fake_llm_client(), scope=None)
    search = next(t for t in tools if t.name == "search_materials")

    result = await search.execute({"query": "anything"})

    assert not result.is_error
    assert result.content == NO_SCOPE_RESULT
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scope.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.scope'`.

- [ ] **Step 3: Create `app/rag/scope.py`:**

```python
"""What a piece of generation may draw on (S26, V05) — decided once, here.

Every grounding path used to assemble its own filter: lessons admitted untagged sources, chat
did not, and the agent's search in a "General — no library grounding" chat searched the whole
library. ``resolve_scope`` is now the only place those decisions are made, so the paths cannot
drift apart again.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import Subject


@dataclass(frozen=True)
class SourceScope:
    """The sources one retrieval may read. Neither a subject nor sources means the learner's
    whole library, which only the debug ``/retrieve`` endpoint asks for."""

    learner_id: uuid.UUID
    subject_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None
    source_ids: tuple[uuid.UUID, ...] = ()
    include_untagged: bool = False
    sources_only: bool = False


async def resolve_scope(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    source_ids: Sequence[uuid.UUID] = (),
) -> SourceScope | None:
    """The scope for a conversation or lesson, or ``None`` for no library at all.

    ``None`` is a General conversation, and callers skip retrieval rather than asking for an
    empty result. Picked sources are the learner's explicit choice, so they turn the untagged
    switch off: they already said which material this is.
    """
    picked = tuple(source_ids)
    if subject_id is None:
        return SourceScope(learner_id=learner_id, source_ids=picked) if picked else None
    subject = await session.get(Subject, subject_id)
    if subject is None:
        raise LookupError(f"subject {subject_id} not found")
    return SourceScope(
        learner_id=learner_id,
        subject_id=subject_id,
        source_ids=picked,
        include_untagged=subject.include_untagged_sources and not picked,
        sources_only=subject.sources_only,
    )
```

- [ ] **Step 4: Change `retrieve()`** in `app/rag/retrieval.py`. Replace the signature and the `scoped` helper; drop the `Sequence` import if unused; add `from app.rag.scope import SourceScope`:

```python
async def retrieve(
    session: AsyncSession,
    llm: LLMClient,
    query: str,
    *,
    scope: SourceScope,
    limit: int = 10,
    candidates: int = 50,
) -> list[RetrievalHit]:
    """Hybrid-retrieve the most relevant chunks for ``query`` within ``scope``.

    The scope is decided by ``app.rag.scope`` (S26), never here: this only applies it. A
    subject admits sources tagged to it, plus untagged ones when the subject opted in; picked
    sources narrow further.
    """
    query = query.strip()
    if not query:
        return []

    space = current_space(llm, dim=get_settings().embed_dim)

    def scoped(stmt: Select) -> Select:
        stmt = stmt.join(Source, Chunk.source_id == Source.id).where(
            Source.learner_id == scope.learner_id
        )
        if scope.source_ids:
            stmt = stmt.where(Chunk.source_id.in_(scope.source_ids))
        if scope.subject_id is not None:
            stmt = stmt.where(
                or_(Source.subject_id == scope.subject_id, Source.subject_id.is_(None))
                if scope.include_untagged
                else Source.subject_id == scope.subject_id
            )
        if scope.topic_id is not None:
            stmt = stmt.where(Source.topic_id == scope.topic_id)
        return stmt
```

(The rest of the function body is unchanged.)

- [ ] **Step 5: Migrate production callers.**

`app/services/content.py` — replace the retrieval block in `generate_block` (the comment + `subject_id = …` + `grounding = await retrieval.retrieve(…)`):

```python
    # Scoped by the KC's subject through the one rule every path uses (S26): its own sources,
    # plus untagged ones only if the subject opted in.
    subject_id = await session.scalar(select(Topic.subject_id).where(Topic.id == kc.topic_id))
    scope = await resolve_scope(session, learner_id=learner_id, subject_id=subject_id)
    assert scope is not None, "a KC always belongs to a subject"
    grounding = await retrieval.retrieve(session, llm, _kc_query(kc), scope=scope, limit=grounding_k)
```

with `from app.rag.scope import resolve_scope`.

`app/services/chat.py` — replace the `hits = [] / grounding = None / if subject_id is not None:` block:

```python
    hits = []
    grounding = None
    scope = await resolve_scope(
        session, learner_id=learner_id, subject_id=subject_id, source_ids=source_ids
    )
    if scope is not None:
        hits = await retrieve(
            session, llm, user_content, scope=scope, limit=get_settings().chat_grounding_limit
        )
        grounding = format_grounding(hits)
```

`app/services/workflow.py` — replace the `grounding = None / if conversation.subject_id is not None:` block the same way, with `step.kc_name` as the query and `subject_id=conversation.subject_id`. Keep `hits` bound on every path that later reads it (initialise `hits = []` before the branch if it is not already).

`app/agent/tools.py` — add a module constant and change `build_tools` / `_search_materials_tool`:

```python
NO_SCOPE_RESULT = (
    "This conversation is not tied to a subject, so none of the learner's materials are in "
    "scope. Answer from general knowledge and say so."
)


def build_tools(
    session: AsyncSession,
    llm: LLMClient,
    *,
    scope: SourceScope | None,
    citations: CitationAccumulator | None = None,
) -> list[Tool]:
    """The tool set for one turn. ``scope`` is ``None`` in a General conversation, where
    ``search_materials`` answers that nothing is in scope rather than searching anything.
    ``citations`` defaults to a fresh, throwaway accumulator when the caller doesn't need to
    read it back — pass one explicitly (``run_agentic_turn`` does) to collect what was cited
    across the whole turn.

    v0 only searches stored materials. External web tools are never registered.
    """
    citations = citations if citations is not None else CitationAccumulator()
    return [_search_materials_tool(session, llm, scope=scope, citations=citations)]
```

In `_search_materials_tool(session, llm, *, scope: SourceScope | None, citations)`, after the empty-query check:

```python
        if scope is None:
            return ToolResult(content=NO_SCOPE_RESULT)
        hits = await retrieve(session, llm, query, scope=scope)
```

(Keep `build_tools`'s existing return list shape — if it currently returns more than `_search_materials_tool`, keep the others and only change the search tool's arguments.)

`app/services/agentic.py` — before `build_tools`:

```python
    scope = await resolve_scope(
        session,
        learner_id=learner_id,
        subject_id=conversation.subject_id,
        source_ids=source_ids,
    )
    tools = build_tools(session, llm, scope=scope, citations=citation_acc)
```

`app/services/onboarding.py` — in the per-source loop:

```python
            hits = await retrieval.retrieve(
                session,
                llm,
                goal,
                scope=SourceScope(learner_id=learner_id, source_ids=(source_id,)),
                limit=3,
            )
```

`app/api/v1/sources.py` — the debug endpoint:

```python
    return await retrieval.retrieve(
        session,
        llm,
        data.query,
        scope=SourceScope(
            learner_id=learner.id,
            subject_id=data.subject_id,
            topic_id=data.topic_id,
            source_ids=(data.source_id,) if data.source_id is not None else (),
        ),
        limit=data.limit,
    )
```

`tests/eval/harness.py:284` — `scope=SourceScope(learner_id=learner.id), limit=case.k`.

- [ ] **Step 6: Migrate test call sites.** Rule: `learner_id=X` → `scope=SourceScope(learner_id=X)`; `learner_id=X, subject_id=S` → `scope=SourceScope(learner_id=X, subject_id=S)`; `…, include_untagged_sources=True` → `include_untagged=True` inside the scope; `source_ids=[a, b]` → `source_ids=(a, b)` inside the scope. Files: `tests/test_retrieval.py` (all `retrieval.retrieve(` calls), `tests/test_v0_web_policy.py:198`. Do **not** touch `memory_retrieval.retrieve` calls (`tests/test_memory_lifecycle.py`, `tests/test_memory_retrieval.py`) — that is a different function.

`tests/test_agent_tools.py`: every `build_tools(db_session, fake_llm_client(), learner_id=L.id)` → `build_tools(db_session, fake_llm_client(), scope=SourceScope(learner_id=L.id))`.

`tests/test_content.py`: lessons now leave untagged sources out by default, and most tests seed untagged sources. Give `_kc` a switch and default it on so the existing tests keep testing what they tested:

```python
async def _kc(
    session: AsyncSession, name: str = "Mitochondria", *, include_untagged: bool = True
) -> KC:
    """A KC in its own subject. ``include_untagged`` defaults on because most tests here seed
    untagged sources as grounding; the S26 tests below turn it off to test the default."""
    subject = Subject(
        slug=f"s-{uuid.uuid4().hex[:8]}",
        name="Biology",
        include_untagged_sources=include_untagged,
    )
```

Replace `test_an_untagged_source_still_grounds_the_block` with these two:

```python
async def test_an_untagged_source_is_left_out_by_default(db_session: AsyncSession) -> None:
    """V05: unassigned material is not silently added. Before S26 it was, for lessons only."""
    learner = await _learner(db_session)
    kc = await _kc(db_session, include_untagged=False)
    untagged = await _source(db_session, learner)
    await _chunk(db_session, untagged, "mitochondria are the powerhouse of the cell", 0)

    block = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert block.grounding_count == 0
    assert block.citations == []


async def test_an_untagged_source_grounds_the_block_once_the_subject_opts_in(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session, include_untagged=True)
    untagged = await _source(db_session, learner)
    chunk = await _chunk(db_session, untagged, "mitochondria are the powerhouse of the cell", 0)

    block = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert [c["chunk_id"] for c in block.citations] == [str(chunk.id)]
```

`tests/test_citation_support.py`: if it defines its own `_kc`, apply the same `include_untagged=True` default to the `Subject` it creates; if it imports from `tests.test_content`, nothing to do.

Add to `tests/test_retrieval.py` (after the existing untagged test, which you migrate per the rule):

```python
async def test_picked_sources_are_all_a_scope_reads(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    picked, other = await _source(db_session, learner), await _source(db_session, learner)
    a = await _chunk(db_session, picked, "calculus limits", ordinal=0)
    await _chunk(db_session, other, "calculus derivatives", ordinal=0)

    hits = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "calculus",
        scope=SourceScope(learner_id=learner.id, source_ids=(picked.id,)),
    )

    assert {h.chunk_id for h in hits} == {a.id}
```

(Adapt `_source`/`_chunk` calls to the helper signatures already in `tests/test_retrieval.py`.)

- [ ] **Step 7: Run tests and the gate**

Run: `uv run pytest tests/test_scope.py tests/test_retrieval.py tests/test_agent_tools.py tests/test_content.py tests/test_citation_support.py tests/test_onboarding.py tests/test_v0_web_policy.py -v` → PASS.
Run: `grep -rn "include_untagged_sources=\|learner_id=.*subject_id=.*retrieve" app tests | grep -v "Subject(\|scope\|models\|schemas\|api/v1/knowledge"` → no stale callers.
Run: `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 8: Commit**

```bash
git add app/rag/scope.py app/rag/retrieval.py app/services/content.py app/services/chat.py app/services/workflow.py app/services/agentic.py app/agent/tools.py app/services/onboarding.py app/api/v1/sources.py tests/eval/harness.py tests/test_scope.py tests/test_retrieval.py tests/test_v0_web_policy.py tests/test_agent_tools.py tests/test_content.py tests/test_citation_support.py
git status
git commit -m "feat(rag): one scope rule for every retrieval path [S26]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: One grounding policy for chat, practice and agent turns [S28]

**Files:**
- Create: `app/services/grounding.py`
- Modify: `app/services/turn_common.py` (remove `GROUNDING_INSTRUCTION`, `format_grounding`), `app/services/chat.py`, `app/services/workflow.py`, `app/services/agentic.py`, `app/agent/tools.py`
- Test: `tests/test_grounding.py` (create)

**Interfaces:**
- Consumes: `SourceScope` (Task 2), `RetrievalHit`.
- Produces:
  ```python
  def instruction(*, sources_only: bool, has_passages: bool) -> str
  def format_grounding(hits: Sequence[RetrievalHit], *, sources_only: bool) -> str
  def policy_note(*, sources_only: bool) -> str
  ```

- [ ] **Step 1: Write the failing tests** — `tests/test_grounding.py`:

```python
"""The one grounding policy (S28): what the tutor is told about its passages, in all four cases."""

import uuid

from app.rag.retrieval import RetrievalHit
from app.services.grounding import format_grounding, instruction, policy_note


def _hit(text: str) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), text=text, provenance={}, score=1.0
    )


def test_with_passages_the_tutor_cites_and_names_what_goes_beyond_them() -> None:
    text = instruction(sources_only=False, has_passages=True)
    assert "cite it inline" in text
    assert "general knowledge" in text
    assert "disagree" in text


def test_sources_only_with_passages_forbids_filling_gaps() -> None:
    text = instruction(sources_only=True, has_passages=True)
    assert "only from these passages" in text
    assert "do not answer it from general knowledge" in text
    assert "disagree" in text


def test_with_nothing_retrieved_the_tutor_says_so_then_answers() -> None:
    text = instruction(sources_only=False, has_passages=False)
    assert "had nothing relevant" in text
    assert "answer from general knowledge" in text


def test_sources_only_with_nothing_retrieved_declines_and_points_at_the_switch() -> None:
    text = instruction(sources_only=True, has_passages=False)
    assert "do not cover this" in text
    assert "turning off sources-only" in text
    assert "Do not answer from general knowledge" in text


def test_passages_are_numbered_and_fenced() -> None:
    section = format_grounding([_hit("alpha"), _hit("beta")], sources_only=False)
    assert "[1] alpha" in section and "[2] beta" in section
    assert "RETRIEVED PASSAGES" in section


def test_an_empty_retrieval_still_tells_the_tutor_what_happened() -> None:
    section = format_grounding([], sources_only=True)
    assert section == instruction(sources_only=True, has_passages=False)


def test_the_agent_note_carries_the_same_rule() -> None:
    assert "only from" in policy_note(sources_only=True)
    assert "general knowledge" in policy_note(sources_only=False)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.grounding'`.

- [ ] **Step 3: Create `app/services/grounding.py`:**

```python
"""What the tutor is told about the learner's passages (S28) — one policy for every turn.

Four situations, one sentence set each: passages or none, sources-only or not. Before this the
chat, practice and agent paths shared only the citation instruction, so an empty retrieval said
nothing at all and the model's fallback to general knowledge was never announced.
"""

from collections.abc import Sequence

from app.agent.untrusted import as_untrusted
from app.rag.retrieval import RetrievalHit

_CITE = (
    "When your answer draws on one of the numbered passages below, cite it inline immediately "
    'after the sentence that uses it, like this: "...as shown here [1]." Only cite a passage '
    "you actually used — never invent a number that isn't listed."
)
_CONFLICT = (
    "If the passages disagree with one another, say so and cite each side rather than "
    "silently choosing one."
)
_BEYOND = (
    "If you add anything the passages do not say, tell the learner in a plain sentence that "
    "this part comes from general knowledge rather than their materials."
)
_ONLY = (
    "This subject is set to sources-only: answer only from these passages. If they do not "
    "cover part of the question, name that part, say their materials do not cover it, and do "
    "not answer it from general knowledge."
)
_NOTHING = (
    "The learner's materials for this subject had nothing relevant to this message. Say so "
    "plainly, then answer from general knowledge."
)
_NOTHING_ONLY = (
    "The learner's materials for this subject had nothing relevant to this message, and the "
    "subject is set to sources-only. Tell them their materials do not cover this, and suggest "
    "adding a source or turning off sources-only for the subject. Do not answer from general "
    "knowledge."
)


def instruction(*, sources_only: bool, has_passages: bool) -> str:
    """The rule for one turn. Callers with no library scope at all never ask."""
    if not has_passages:
        return _NOTHING_ONLY if sources_only else _NOTHING
    return " ".join((_CITE, _CONFLICT, _ONLY if sources_only else _BEYOND))


def format_grounding(hits: Sequence[RetrievalHit], *, sources_only: bool) -> str:
    """The grounding section of a system prompt: the rule, then the passages fenced as data.

    Never empty for a scoped turn — "nothing matched" is something the tutor must be told, or
    it answers from general knowledge without saying so.
    """
    rule = instruction(sources_only=sources_only, has_passages=bool(hits))
    if not hits:
        return rule
    passages = "\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
    # Fenced as data (S31): a passage is whatever someone uploaded, and an uploaded document
    # can contain a sentence addressed to the model.
    return f"{rule}\n\n{as_untrusted('RETRIEVED PASSAGES', passages)}"


def policy_note(*, sources_only: bool) -> str:
    """The same rule for the agent, whose passages arrive in ``search_materials`` results."""
    found = _ONLY if sources_only else _BEYOND
    missing = (
        "If a search finds nothing relevant, tell the learner their materials do not cover "
        "this and do not answer from general knowledge."
        if sources_only
        else "If a search finds nothing relevant, say so, then answer from general knowledge."
    )
    return " ".join(("Passages come from the search_materials tool.", found, _CONFLICT, missing))
```

Note `_ONLY` says "only from these passages", which the sources-only `policy_note` test matches via "only from".

- [ ] **Step 4: Move callers onto it.**

- In `app/services/turn_common.py`, delete `GROUNDING_INSTRUCTION` and `format_grounding` (and the `as_untrusted` import if now unused). Keep `extract_citations`.
- `app/services/chat.py`: import `format_grounding` from `app.services.grounding` instead of `turn_common`; the call becomes `format_grounding(hits, sources_only=scope.sources_only)`.
- `app/services/workflow.py`: same import change and call.
- `app/agent/tools.py`: import from `app.services.grounding`; inside the search tool keep the "No relevant passages found in the learner's materials." result when `citations.hits` is empty, else `format_grounding(citations.hits, sources_only=scope.sources_only)`.
- `app/services/agentic.py`: the system prompt carries the policy when there is a scope:

```python
        "system": learner_context.compose(
            AGENTIC_SYSTEM_PROMPT,
            context,
            extra=[policy_note(sources_only=scope.sources_only)] if scope is not None else [],
        ),
```

- [ ] **Step 5: Add one practice test and one chat test** that pin the shared text. Find the existing tests that capture the system prompt sent by a chat turn (`grep -n "system" tests/test_chat*.py tests/test_workflow*.py | head`) and follow their spy pattern. Add to the chat test module:

```python
async def test_a_subject_chat_with_nothing_retrieved_is_told_so(...) -> None:
    # A subject conversation, no sources seeded: the system prompt contains
    # instruction(sources_only=False, has_passages=False) verbatim.
    ...
    assert instruction(sources_only=False, has_passages=False) in system
```

and to the practice (workflow) test module the same assertion for a sources-only subject (`Subject(..., sources_only=True)`), asserting `instruction(sources_only=True, has_passages=False) in system`. Fill the `...` with that module's own conversation/learner/subject fixtures — each module already builds a subject conversation for its grounding tests.

- [ ] **Step 6: Run tests and the gate**

Run: `uv run pytest tests/test_grounding.py tests/test_agent_tools.py -v` then `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 7: Commit**

```bash
git add app/services/grounding.py app/services/turn_common.py app/services/chat.py app/services/workflow.py app/services/agentic.py app/agent/tools.py tests/test_grounding.py <the chat and workflow test files you edited>
git status
git commit -m "feat(tutor): one grounding policy, including sources-only and empty retrieval [S28]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Lessons follow the policy; sources-only with nothing to cite generates nothing [S26]

**Files:**
- Modify: `app/services/content.py` (prompts, `generate_block`, new `NoSourceCoverage`), `app/api/v1/content.py`
- Test: `tests/test_content.py`

**Interfaces:**
- Consumes: `SourceScope.sources_only` (Task 2).
- Produces: `class NoSourceCoverage(LookupError)` in `app/services/content.py` with attribute `kc_id: uuid.UUID`; `POST /content/generate` returns 422 `{"detail": "Your sources for this subject don't cover this concept."}` when raised.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_content.py`, under the S28 section):

```python
async def test_sources_only_with_nothing_retrieved_generates_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    subject = await db_session.scalar(
        select(Subject).join(Topic, Topic.subject_id == Subject.id).where(Topic.id == kc.topic_id)
    )
    subject.sources_only = True
    await db_session.flush()
    client = _client()
    seen = _spy_on_prompts(client, monkeypatch)

    with pytest.raises(svc.NoSourceCoverage):
        await svc.generate_block(
            db_session, client, learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
        )

    assert seen == [], "no model call was made"
    assert await _count_blocks(db_session, kc) == 0


async def test_sources_only_is_the_rule_sent_with_grounding(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    subject = await db_session.scalar(
        select(Subject).join(Topic, Topic.subject_id == Subject.id).where(Topic.id == kc.topic_id)
    )
    subject.sources_only = True
    await db_session.flush()
    await _seed_grounding(db_session, learner)
    client = _client()
    seen = _spy_on_prompts(client, monkeypatch)

    await svc.generate_block(
        db_session, client, learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    system, _ = seen[0]
    assert "Use ONLY the numbered context snippets" in system


async def test_switching_to_sources_only_does_not_reuse_a_block_written_without_it(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, learner)
    first = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )
    subject = await db_session.scalar(
        select(Subject).join(Topic, Topic.subject_id == Subject.id).where(Topic.id == kc.topic_id)
    )
    subject.sources_only = True
    await db_session.flush()

    second = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
    )

    assert second.id != first.id


async def test_the_api_says_the_sources_do_not_cover_the_concept(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner, fake_llm: None
) -> None:
    kc = await _kc(db_session)
    subject = await db_session.scalar(
        select(Subject).join(Topic, Topic.subject_id == Subject.id).where(Topic.id == kc.topic_id)
    )
    subject.sources_only = True
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/content/generate", json={"kc_id": str(kc.id), "type": "lesson"}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Your sources for this subject don't cover this concept."
```

Adjust imports (`select`, `AsyncClient`) to what the module already imports; if `_count_blocks` returns `None` for zero, assert `in (0, None)`. If the existing API tests in this file use a different fixture name for the signed-in learner or the fake LLM, use those.

Also update `test_with_grounding_the_source_only_instruction_is_the_one_sent` (the normal-mode test): replace `assert "ONLY the numbered context snippets" in system` with `assert "say plainly in the body which part" in system` and add `assert "Use ONLY" not in system`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_content.py -k "sources_only or source_only or cover" -v`
Expected: FAIL — `AttributeError: module 'app.services.content' has no attribute 'NoSourceCoverage'`.

- [ ] **Step 3: Implement in `app/services/content.py`.** Replace `_SYSTEM_PROMPT` with a template carrying the scope rule:

```python
_SYSTEM_PROMPT = (
    "You are an expert instructional author. Write {guidance} for the learning objective from "
    "the numbered context snippets. Ground every claim you can in the context and cite the "
    "snippets you used by their index. {scope_rule} If the snippets disagree with one another, "
    "say so in the body and cite both rather than silently choosing one. Respond with ONLY a "
    "JSON object of the form "
    '{{"body": "<the content>", "citations": [<indices of snippets used>]}} and nothing else.'
)

# The two scope rules (S26). Normal mode may fill a gap from general knowledge but has to say
# which part that is; sources-only names the gap instead of filling it.
_SUPPLEMENT_RULE = (
    "Where the snippets leave a gap the objective needs, you may fill it from general "
    "knowledge, but say plainly in the body which part does not come from the learner's "
    "materials."
)
_SOURCES_ONLY_RULE = (
    "Use ONLY the numbered context snippets: where they leave a gap, say in the body what they "
    "do not cover rather than filling it from general knowledge."
)


class NoSourceCoverage(LookupError):
    """Sources-only, and nothing in the subject's sources matched this KC (S26).

    Raised before any model call: there is nothing the block would be allowed to say.
    """

    def __init__(self, kc_id: uuid.UUID) -> None:
        super().__init__(f"no source coverage for KC {kc_id}")
        self.kc_id = kc_id
```

In `generate_block`, after retrieval:

```python
    if not grounding and scope.sources_only:
        raise NoSourceCoverage(kc_id)
    role = _ROLE_BY_TYPE[block_type]
    if grounding:
        rule = _SOURCES_ONLY_RULE if scope.sources_only else _SUPPLEMENT_RULE
        system = _SYSTEM_PROMPT.format(guidance=_GUIDANCE[block_type], scope_rule=rule)
    else:
        system = _UNGROUNDED_SYSTEM_PROMPT.format(guidance=_GUIDANCE[block_type])
```

(replacing the old `template = …` / `system = template.format(…)` lines). The cache key already hashes `system`, so a settings change produces a new key.

In `app/api/v1/content.py` `generate_content`, wrap both service calls:

```python
    try:
        if data.type is not None:
            block = await svc.generate_block(
                session, llm, learner_id=learner.id, kc_id=data.kc_id, block_type=data.type
            )
            return [block]
        return await svc.assemble(session, llm, learner_id=learner.id, kc_id=data.kc_id)
    except svc.NoSourceCoverage as err:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Your sources for this subject don't cover this concept.",
        ) from err
```

- [ ] **Step 4: Run tests and the gate**

Run: `uv run pytest tests/test_content.py tests/test_citation_support.py -v` → PASS; then `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/content.py app/api/v1/content.py tests/test_content.py
git status
git commit -m "feat(content): lessons follow sources-only and say where they go beyond sources [S26]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Record grounding per reply and derive coverage [S28]

**Files:**
- Create: `app/rag/coverage.py`
- Modify: `app/services/turn_common.py` (`add_message`), `app/services/chat.py`, `app/services/workflow.py`, `app/services/agentic.py`, `app/schemas/chat.py` (`MessageRead`), `app/schemas/content.py` (`ContentBlockRead`), `frontend/src/api/schema.d.ts` (regenerated)
- Test: `tests/test_coverage.py` (create), plus one assertion each in the chat and agentic turn tests

**Interfaces:**
- Consumes: `Message.grounding_count` (Task 1), `scope` in each turn (Task 2).
- Produces:
  ```python
  class Coverage(StrEnum): CITED = "cited"; RETRIEVED_NOT_CITED = "retrieved_not_cited"; NONE = "none"
  def coverage(grounding_count: int | None, citations: Sequence[Mapping]) -> Coverage | None
  def cited_source_count(citations: Sequence[Mapping]) -> int
  ```
  `MessageRead.grounding_count: int | None`, `MessageRead.coverage: Coverage | None` (computed), `MessageRead.cited_source_count: int` (computed); same two computed fields on `ContentBlockRead`. `add_message(..., grounding_count: int | None = None)`.

Note: the spec placed `Coverage` in `app/services/grounding.py`; it lives in `app/rag/coverage.py` instead so the schemas can import it without importing a service module.

- [ ] **Step 1: Write the failing tests** — `tests/test_coverage.py`:

```python
"""Coverage is derived from what the server recorded, never from the model's say-so (S28)."""

from app.rag.coverage import Coverage, cited_source_count, coverage
from app.schemas.chat import MessageRead

_CITE = {"marker": 1, "chunk_id": "c1", "source_id": "s1"}


def test_no_scope_has_no_label() -> None:
    assert coverage(None, []) is None


def test_a_cited_passage_is_cited() -> None:
    assert coverage(3, [_CITE]) is Coverage.CITED


def test_passages_offered_but_not_cited() -> None:
    assert coverage(3, []) is Coverage.RETRIEVED_NOT_CITED


def test_nothing_offered() -> None:
    assert coverage(0, []) is Coverage.NONE


def test_sources_are_counted_once_each() -> None:
    twice = [_CITE, {**_CITE, "marker": 2, "chunk_id": "c2"}, {**_CITE, "source_id": "s2"}]
    assert cited_source_count(twice) == 2


def test_the_message_schema_exposes_the_derived_label() -> None:
    import uuid

    read = MessageRead(
        id=uuid.uuid4(),
        role="assistant",
        content="x",
        model=None,
        citations=[_CITE],
        grounding_count=2,
    )
    dumped = read.model_dump()
    assert dumped["coverage"] == "cited"
    assert dumped["cited_source_count"] == 1


def test_a_message_from_before_s28_has_no_label() -> None:
    import uuid

    read = MessageRead(id=uuid.uuid4(), role="assistant", content="x", model=None, citations=[])
    assert read.model_dump()["coverage"] is None
```

(If `MessageRead` has other required fields, pass them with the values the existing chat schema tests use.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_coverage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.rag.coverage'`.

- [ ] **Step 3: Create `app/rag/coverage.py`:**

```python
"""How much of a reply the learner's sources carried, as far as the server can know (S28).

Derived only from what was recorded — how many passages were offered and which markers the
reply actually cited — so the label cannot be talked into anything by the model. "Partly from
your sources" is deliberately absent: telling partial from full coverage is a judgement about
the text, and the tutor's own sentence carries it.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum


class Coverage(StrEnum):
    CITED = "cited"
    RETRIEVED_NOT_CITED = "retrieved_not_cited"
    NONE = "none"


def coverage(grounding_count: int | None, citations: Sequence[Mapping]) -> Coverage | None:
    """``None`` when there was no library scope, or the row predates the count."""
    if grounding_count is None:
        return None
    if citations:
        return Coverage.CITED
    return Coverage.RETRIEVED_NOT_CITED if grounding_count > 0 else Coverage.NONE


def cited_source_count(citations: Sequence[Mapping]) -> int:
    """Distinct sources cited — two passages from one book are one source to the learner."""
    return len({c["source_id"] for c in citations if "source_id" in c})
```

- [ ] **Step 4: Schemas.** In `app/schemas/chat.py`, `MessageRead` (import `computed_field` from pydantic and `Coverage, coverage, cited_source_count` from `app.rag.coverage`):

```python
    # Passages offered for this reply (S28); NULL = no library scope, or written before S28.
    grounding_count: int | None = None

    @computed_field
    @property
    def coverage(self) -> Coverage | None:
        return coverage(self.grounding_count, self.citations)

    @computed_field
    @property
    def cited_source_count(self) -> int:
        return cited_source_count(self.citations)
```

The property names shadow the imported functions inside the class body; import the module instead to avoid that: `from app.rag import coverage as coverage_rules` and call `coverage_rules.coverage(...)` / `coverage_rules.cited_source_count(...)`, with `Coverage` imported by name. Add the same two computed fields to `ContentBlockRead` in `app/schemas/content.py` (it already has `grounding_count`).

- [ ] **Step 5: Persist the count.** `app/services/turn_common.py` `add_message` gains `grounding_count: int | None = None` and passes it into `Message(...)`. Then:
  - `app/services/chat.py`: the assistant `add_message` gets `grounding_count=len(hits) if scope is not None else None`.
  - `app/services/workflow.py`: on the fresh-start round that retrieved, `grounding_count=len(hits) if scope is not None else None`; on a resumed round (no retrieval this round) `None`. Bind a `grounding_count: int | None = None` local before the start/resume branch and set it where `hits` is set.
  - `app/services/agentic.py`: `grounding_count=len(citation_acc.hits) if scope is not None else None`.

- [ ] **Step 6: Turn tests.** In the chat turn test module, add a test: a subject conversation with one matching source; after the turn, the assistant `Message.grounding_count` is `1`; and a General conversation's assistant message has `grounding_count is None`. In the agentic test module, a General conversation's assistant message has `grounding_count is None`. Use each module's existing turn-driving helpers.

- [ ] **Step 7: Run tests and the gate**

Run: `uv run pytest tests/test_coverage.py -v` and the edited turn test modules → PASS.
Run: `uv run poe api-types` then `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 8: Commit**

```bash
git add app/rag/coverage.py app/services/turn_common.py app/services/chat.py app/services/workflow.py app/services/agentic.py app/schemas/chat.py app/schemas/content.py frontend/src/api/schema.d.ts tests/test_coverage.py <the turn test files you edited>
git status
git commit -m "feat(chat): record grounding per reply and derive its coverage [S28]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Coverage chip under tutor replies [S28]

**Files:**
- Create: `frontend/src/components/chat/CoverageChip.tsx`, `frontend/src/components/chat/CoverageChip.test.tsx`
- Modify: `frontend/src/components/chat/MessageBlock.tsx`, `frontend/src/components/chat/MessageList.tsx`

**Interfaces:**
- Consumes: `MessageRead.coverage` (`"cited" | "retrieved_not_cited" | "none" | null`) and `MessageRead.cited_source_count` from `schema.d.ts` (Task 5).
- Produces: `CoverageChip({ coverage, sourceCount })`.

- [ ] **Step 1: Write the failing test** — `CoverageChip.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CoverageChip } from "./CoverageChip";

describe("CoverageChip", () => {
  it("counts the sources a reply cited", () => {
    render(<CoverageChip coverage="cited" sourceCount={2} />);
    expect(screen.getByText("Draws on 2 of your sources")).toBeInTheDocument();
  });

  it("reads naturally for a single source", () => {
    render(<CoverageChip coverage="cited" sourceCount={1} />);
    expect(screen.getByText("Draws on one of your sources")).toBeInTheDocument();
  });

  it("says when materials were searched but not used", () => {
    render(<CoverageChip coverage="retrieved_not_cited" sourceCount={0} />);
    expect(screen.getByText("Your materials were searched but not used")).toBeInTheDocument();
  });

  it("says when nothing came from the learner's materials", () => {
    render(<CoverageChip coverage="none" sourceCount={0} />);
    expect(screen.getByText("Not from your materials")).toBeInTheDocument();
  });

  it("renders nothing without a scope", () => {
    const { container } = render(<CoverageChip coverage={null} sourceCount={0} />);
    expect(container).toBeEmptyDOMElement();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/chat/CoverageChip.test.tsx`
Expected: FAIL — cannot resolve `./CoverageChip`.

- [ ] **Step 3: Implement** `CoverageChip.tsx`:

```tsx
import { BookOpen } from "lucide-react";

export type Coverage = "cited" | "retrieved_not_cited" | "none";

/** How much of a reply the learner's sources carried (S28). The server derives it from what
 * was offered and what was cited, never from the model's own account; "partly" is left to the
 * tutor's words because only the text can say it. Null — a General chat, or a reply from
 * before this was recorded — shows nothing rather than guessing. */
export function CoverageChip({
  coverage,
  sourceCount,
}: {
  coverage: Coverage | null | undefined;
  sourceCount: number;
}) {
  if (!coverage) return null;
  const label =
    coverage === "cited"
      ? sourceCount === 1
        ? "Draws on one of your sources"
        : `Draws on ${sourceCount} of your sources`
      : coverage === "retrieved_not_cited"
        ? "Your materials were searched but not used"
        : "Not from your materials";
  return (
    <span className="text-caption text-base-content/60 flex items-center gap-1">
      <BookOpen size={12} />
      {label}
    </span>
  );
}
```

- [ ] **Step 4: Wire it.** `MessageBlock` gains optional props `coverage?: Coverage | null` and `sourceCount?: number` (default `0`), rendering `<CoverageChip coverage={coverage} sourceCount={sourceCount} />` below `RichText` in the assistant branch only, and not while `streaming`. In `MessageList`, pass `coverage={m.coverage}` and `sourceCount={m.cited_source_count}` to the persisted-message `MessageBlock` (not to the pending ones).

- [ ] **Step 5: Run tests and the build**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build` → green.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/chat/CoverageChip.tsx frontend/src/components/chat/CoverageChip.test.tsx frontend/src/components/chat/MessageBlock.tsx frontend/src/components/chat/MessageList.tsx
git status
git commit -m "feat(web): show how much of each reply the learner's sources carried [S28]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Source switches on the Lessons page and an upload hint [S26]

**Files:**
- Create: `frontend/src/components/lessons/SourceSettingsPanel.tsx`, `frontend/src/components/lessons/SourceSettingsPanel.test.tsx`
- Modify: `frontend/src/api/hooks.ts`, `frontend/src/pages/Lessons.tsx`, `frontend/src/components/uploads/UploadForm.tsx`

**Interfaces:**
- Consumes: `PATCH /api/v1/subjects/{subject_id}/source-settings`, `SubjectRead.include_untagged_sources`, `SubjectRead.sources_only` (Task 1).
- Produces: `useUpdateSourceSettings()`; `SourceSettingsPanel({ includeUntagged, sourcesOnly, onChange, disabled })` where `onChange(patch: { include_untagged_sources?: boolean; sources_only?: boolean })`.

- [ ] **Step 1: Write the failing test** — `SourceSettingsPanel.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourceSettingsPanel } from "./SourceSettingsPanel";

describe("SourceSettingsPanel", () => {
  it("shows both switches in their current state", () => {
    render(<SourceSettingsPanel includeUntagged={false} sourcesOnly onChange={() => {}} />);
    expect(screen.getByRole("checkbox", { name: /untagged materials/i })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /only from my sources/i })).toBeChecked();
  });

  it("reports one switch at a time", async () => {
    const onChange = vi.fn();
    render(<SourceSettingsPanel includeUntagged={false} sourcesOnly={false} onChange={onChange} />);
    await userEvent.click(screen.getByRole("checkbox", { name: /untagged materials/i }));
    expect(onChange).toHaveBeenCalledWith({ include_untagged_sources: true });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/lessons/SourceSettingsPanel.test.tsx`
Expected: FAIL — cannot resolve `./SourceSettingsPanel`.

- [ ] **Step 3: Implement** `SourceSettingsPanel.tsx`:

```tsx
export type SourceSettingsPatch = { include_untagged_sources?: boolean; sources_only?: boolean };

/** What this subject may draw on (S26, V05). Presentational: the Lessons page owns the
 * request, so the switches can be tested without a server. */
export function SourceSettingsPanel({
  includeUntagged,
  sourcesOnly,
  onChange,
  disabled,
}: {
  includeUntagged: boolean;
  sourcesOnly: boolean;
  onChange: (patch: SourceSettingsPatch) => void;
  disabled?: boolean;
}) {
  return (
    <section className="flex max-w-2xl flex-col gap-3">
      <h2 className="text-h3">Sources</h2>
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          className="toggle toggle-sm mt-0.5"
          checked={includeUntagged}
          disabled={disabled}
          onChange={(e) => onChange({ include_untagged_sources: e.target.checked })}
        />
        <span className="flex flex-col">
          <span className="text-body">Also use my untagged materials</span>
          <span className="text-caption text-base-content/60">
            Uploads without a subject are left out unless you turn this on.
          </span>
        </span>
      </label>
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          className="toggle toggle-sm mt-0.5"
          checked={sourcesOnly}
          disabled={disabled}
          onChange={(e) => onChange({ sources_only: e.target.checked })}
        />
        <span className="flex flex-col">
          <span className="text-body">Teach only from my sources</span>
          <span className="text-caption text-base-content/60">
            Guru says when your sources don't cover something instead of filling in from general
            knowledge.
          </span>
        </span>
      </label>
    </section>
  );
}
```

(The accessible names come from the label text: "Also use my untagged materials…" matches `/untagged materials/i`; "Teach only from my sources…" matches `/only from my sources/i`.)

- [ ] **Step 4: Hook** in `frontend/src/api/hooks.ts`, after `useSubjects`:

```ts
/** The subject's source switches (S26); refreshes the subject list the Lessons page reads. */
export function useUpdateSourceSettings(subjectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (patch: { include_untagged_sources?: boolean; sources_only?: boolean }) => {
      const { data, error } = await api.PATCH("/api/v1/subjects/{subject_id}/source-settings", {
        params: { path: { subject_id: subjectId } },
        body: patch,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["subjects"] });
    },
  });
}
```

- [ ] **Step 5: Lessons page.** In `frontend/src/pages/Lessons.tsx`, add a small wrapper component in the same file and render it inside the owned-subject fragment, before `CurriculumIssuesPanel`:

```tsx
function SubjectSources({ subject }: { subject: Subject }) {
  const update = useUpdateSourceSettings(subject.id);
  return (
    <SourceSettingsPanel
      includeUntagged={subject.include_untagged_sources ?? false}
      sourcesOnly={subject.sources_only ?? false}
      onChange={(patch) => update.mutate(patch)}
      disabled={update.isPending}
    />
  );
}
```

with `<SubjectSources key={`sources-${selected.id}`} subject={selected} />`. Take the `Subject` type from wherever `SubjectPicker` imports it (`grep -n "Subject" frontend/src/components/SubjectPicker.tsx`).

- [ ] **Step 6: Upload hint.** In `UploadForm.tsx`, under the subject `<select>`, add:

```tsx
<p className="text-caption text-base-content/50">
  Without a subject, a file is only used by subjects that opt in to untagged materials.
</p>
```

Run `grep -rn "getByText\|toHaveTextContent" frontend/src/components/uploads/UploadForm.test.tsx` to make sure no existing assertion is broken by the extra text.

- [ ] **Step 7: Run tests and the build**

Run: `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build` → green.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/lessons/SourceSettingsPanel.tsx frontend/src/components/lessons/SourceSettingsPanel.test.tsx frontend/src/api/hooks.ts frontend/src/pages/Lessons.tsx frontend/src/components/uploads/UploadForm.tsx
git status
git commit -m "feat(web): per-subject source switches and an untagged-upload hint [S26]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Tracker and project notes [S26]

**Files:**
- Modify: `docs/guru-suggestions-tracker.md` (rows S26, S28, S80), `CLAUDE.md` (Key Technical Decisions)

- [ ] **Step 1: Tracker.**
  - S26 → **Completed**. Notes: one scope rule (`app/rag/scope.py`) for lessons, chat, practice, agent search, onboarding and debug retrieval; untagged sources opt-in per subject; per-subject sources-only; General chats reach no library (closes the agent-search leak); lessons in sources-only with nothing retrieved generate nothing (422). Evidence links: `app/rag/scope.py`, `tests/test_scope.py`, `tests/test_source_settings.py`.
  - S28 → stays **Partial**. Done: one grounding policy (`app/services/grounding.py`) including empty retrieval and sources-only; per-reply `grounding_count`; server-derived coverage label in chat. Remaining: support-checker accuracy (S59); showing lesson coverage once a lesson-block viewer exists.
  - S80: add "`fully_sourced` — would sharpen the coverage label into full vs partial (S28); shadow first" as a candidate question.
- [ ] **Step 2: CLAUDE.md** — add one bullet to Key Technical Decisions after the S25 bullet:

```markdown
- **One source scope, one grounding policy** (S26/S28) — `resolve_scope` in `app/rag/scope.py`
  decides what any generation may read (a subject's own sources; untagged ones only if the
  subject opts in; a General chat reads none), and `app/services/grounding.py` decides what the
  tutor is told, including sources-only and "nothing matched". The coverage label on a reply is
  derived from what was offered and cited, never from the model's own account.
```

- [ ] **Step 3: Gate and commit**

Run: `uv run poe check` → green.

```bash
git add docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record source scope and grounding policy in the tracker [S26]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
