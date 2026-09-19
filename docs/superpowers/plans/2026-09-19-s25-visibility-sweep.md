# S25a Visibility Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every API operation that accepts a subject, topic, KC or item id refuses ids the caller
cannot see. Refusals are indistinguishable from an id that does not exist. A test fails if a new
operation skips the check.

**Architecture:** One visibility gate in `app/services/knowledge.py` (`NotVisible` plus three
resolvers), mapped to a single 404 by a global FastAPI exception handler. The existing knowledge
and notes helpers are re-expressed on it. The four ungated route families call it: lesson plan,
analytics, content, and sources/retrieval. A table-driven, cross-learner test drives every
operation through the real API. An OpenAPI-walking drift guard keeps that table complete.

**Tech Stack:** FastAPI, async SQLAlchemy 2, pytest-asyncio, httpx `ASGITransport`, uv + poethepoet.

**Spec:** `docs/superpowers/specs/2026-09-19-s25-visibility-sweep-design.md`

## Global Constraints

- Python 3.13; ruff line length 100; run commands via `uv run`.
- Every commit leaves these green: `uv run poe check` (lint + ty + tests), `uv run poe format-check`,
  `uv run poe api-contract`. No OpenAPI change is expected; the 404s carry no response model.
- Commit subjects end with `[S25]`. Commit bodies end with the line
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Stage files **by explicit path only**. Never `git add -A` / `git add .` / `git commit -a`.
- Never stage `docs/guru-suggestions-tracker.md` (the owner's uncommitted rewrite) or
  `.claude/settings.json`.
- Never `git reset`, `git commit --amend`, `git rebase`, `git stash`, `git push`, or force anything.
  If a commit is wrong, make a new commit.
- A missing row and another learner's private row must produce byte-identical responses.
  Messages name only ids the caller sent.
- `test_retrieval_eval_gate` is a known intermittent failure (S76). If it is the *only* failure,
  re-run it alone; a pass on re-run is acceptable, and must be reported.

## File map

| File | Change |
| --- | --- |
| `app/services/knowledge.py` | Add `NotVisible`, `require_visible_subject/_topic/_kc`; `resolve_source_scope` gains `learner_id` and scrubbed messages |
| `app/main.py` | Global `NotVisible` → 404 handler |
| `app/api/v1/knowledge.py` | Helpers and `list_kcs`/`get_kc` re-expressed on the resolvers |
| `app/api/v1/notes.py`, `app/services/notes.py` | Topic/subject gates re-expressed on the resolvers |
| `app/api/v1/lesson_plan.py`, `app/api/v1/analytics.py` | Gate added |
| `app/api/v1/content.py` | Gate added |
| `app/services/ingestion.py`, `app/api/v1/sources.py` | Scope resolution passes the learner; retrieve/list gate supplied ids |
| `tests/test_visibility_gate.py` (new) | Resolver unit tests + handler |
| `tests/test_visibility_sweep.py` (new) | Cross-learner table, scrubbed-message tests, drift guard |
| `tests/test_source_scope.py` | Updated for the new `resolve_source_scope` signature and message |

---

### Task 1: The visibility gate

**Files:**
- Modify: `app/services/knowledge.py` (add after `is_writable_by`, around line 395)
- Modify: `app/main.py` (handler after `app = FastAPI(...)`, line 46)
- Modify: `app/api/v1/knowledge.py:55-100` (helpers), `:301-313` (`list_kcs`, `get_kc`)
- Modify: `app/api/v1/notes.py` (`_topic_404`, `notes_index`)
- Modify: `app/services/notes.py:66-71` (`_require_visible_topic`)
- Create: `tests/test_visibility_gate.py`

**Interfaces:**
- Produces, in `app.services.knowledge`:
  - `class NotVisible(LookupError)` with attribute `kind: str`; `str(exc) == f"{kind} not found"`
  - `async def require_visible_subject(session: AsyncSession, subject_id: uuid.UUID, learner_id: uuid.UUID) -> Subject`
  - `async def require_visible_topic(session: AsyncSession, topic_id: uuid.UUID, learner_id: uuid.UUID) -> tuple[Subject, Topic]`
  - `async def require_visible_kc(session: AsyncSession, kc_id: uuid.UUID, learner_id: uuid.UUID) -> tuple[Subject, KC]`
- Produces, in `app.main`: any `NotVisible` escaping a route becomes `404 {"detail": str(exc)}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_visibility_gate.py`:

```python
"""The one gate every graph id passes through (S25).

A missing id and another learner's private id must be the same event to the caller, so they are
the same exception with the same message. Curated subjects (no owner) are visible to everyone.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.services import knowledge as svc


async def _graph(session: AsyncSession, owner: uuid.UUID | None) -> tuple[Subject, Topic, KC]:
    tag = uuid.uuid4().hex[:8]
    subject = Subject(slug=f"s-{tag}", name=f"Subject {tag}", owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{tag}", name="Topic")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{tag}", name="Component")
    session.add(kc)
    await session.flush()
    return subject, topic, kc


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"gate-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_own_and_curated_graphs_resolve(db_session: AsyncSession) -> None:
    me = await _learner(db_session)
    for owner in (me.id, None):
        subject, topic, kc = await _graph(db_session, owner)

        assert await svc.require_visible_subject(db_session, subject.id, me.id) == subject
        assert await svc.require_visible_topic(db_session, topic.id, me.id) == (subject, topic)
        assert await svc.require_visible_kc(db_session, kc.id, me.id) == (subject, kc)


@pytest.mark.parametrize("kind", ["subject", "topic", "kc"])
async def test_a_strangers_private_id_and_a_missing_id_are_the_same_refusal(
    db_session: AsyncSession, kind: str
) -> None:
    me = await _learner(db_session)
    stranger = await _learner(db_session)
    subject, topic, kc = await _graph(db_session, stranger.id)
    theirs = {"subject": subject.id, "topic": topic.id, "kc": kc.id}[kind]
    resolve = {
        "subject": svc.require_visible_subject,
        "topic": svc.require_visible_topic,
        "kc": svc.require_visible_kc,
    }[kind]

    with pytest.raises(svc.NotVisible) as foreign:
        await resolve(db_session, theirs, me.id)
    with pytest.raises(svc.NotVisible) as missing:
        await resolve(db_session, uuid.uuid4(), me.id)

    assert str(foreign.value) == str(missing.value) == f"{kind} not found"
    assert foreign.value.kind == missing.value.kind == kind


async def test_the_refusal_reaches_the_client_as_one_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"/api/v1/subjects/{uuid.uuid4()}")

    assert r.status_code == 404
    assert r.json() == {"detail": "subject not found"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_visibility_gate.py -q`
Expected: FAIL with `AttributeError: module 'app.services.knowledge' has no attribute 'require_visible_subject'`.
The last test passes already; `_visible_subject` produces the same body today.

- [ ] **Step 3: Implement the gate in `app/services/knowledge.py`**

Insert directly after `is_writable_by` (which ends around line 395):

```python
class NotVisible(LookupError):
    """A graph id that names nothing, or names another learner's private material (S25).

    One exception for both, on purpose. A caller who could tell them apart could map somebody
    else's curriculum one id at a time. ``kind`` is what the id was meant to name, and the
    message is the entire 404 body a route sends (see the handler in ``app.main``).
    """

    def __init__(self, kind: str) -> None:
        super().__init__(f"{kind} not found")
        self.kind = kind


async def require_visible_subject(
    session: AsyncSession, subject_id: uuid.UUID, learner_id: uuid.UUID
) -> Subject:
    """The subject, if ``learner_id`` may see it; otherwise ``NotVisible``."""
    subject = await session.get(Subject, subject_id)
    if subject is None or not is_visible_to(subject, learner_id):
        raise NotVisible("subject")
    return subject


async def require_visible_topic(
    session: AsyncSession, topic_id: uuid.UUID, learner_id: uuid.UUID
) -> tuple[Subject, Topic]:
    """The topic and the subject that decides its visibility, or ``NotVisible``."""
    row = (
        await session.execute(
            select(Subject, Topic)
            .join(Topic, Topic.subject_id == Subject.id)
            .where(Topic.id == topic_id)
        )
    ).first()
    if row is None or not is_visible_to(row[0], learner_id):
        raise NotVisible("topic")
    return row[0], row[1]


async def require_visible_kc(
    session: AsyncSession, kc_id: uuid.UUID, learner_id: uuid.UUID
) -> tuple[Subject, KC]:
    """The component and the subject that decides its visibility, or ``NotVisible``."""
    row = (
        await session.execute(
            select(Subject, KC)
            .join(Topic, Topic.subject_id == Subject.id)
            .join(KC, KC.topic_id == Topic.id)
            .where(KC.id == kc_id)
        )
    ).first()
    if row is None or not is_visible_to(row[0], learner_id):
        raise NotVisible("kc")
    return row[0], row[1]
```

- [ ] **Step 4: Register the handler in `app/main.py`**

Add the imports next to the existing ones:

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.services.knowledge import NotVisible
```

Directly after `app = FastAPI(title="Guru API", version="0.1.0", lifespan=lifespan)`:

```python
@app.exception_handler(NotVisible)
async def _not_visible(_request: Request, exc: NotVisible) -> JSONResponse:
    """The one 404 for a graph id the caller cannot see (S25). See ``knowledge.NotVisible``."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})
```

- [ ] **Step 5: Re-express the knowledge API helpers**

In `app/api/v1/knowledge.py`, keep each docstring and replace the bodies:

```python
async def _visible_subject(session, subject_id: uuid.UUID, learner) -> Subject:
    # (existing docstring unchanged)
    return await svc.require_visible_subject(session, subject_id, learner.id)
```

```python
async def _writable_subject_of_topic(session, topic_id: uuid.UUID, learner) -> Subject:
    subject, _ = await svc.require_visible_topic(session, topic_id, learner.id)
    _require_writable(subject, learner)
    return subject


async def _writable_subject_of_kc(session, kc_id: uuid.UUID, learner, *, missing: str) -> Subject:
    # ``missing`` survives because the two ends of an edge answer differently: "kc not found"
    # for the dependent, "prerequisite kc not found" for the prerequisite.
    try:
        subject, _ = await svc.require_visible_kc(session, kc_id, learner.id)
    except svc.NotVisible as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing) from exc
    _require_writable(subject, learner)
    return subject
```

Replace the bodies' visibility checks in `list_kcs` and `get_kc`:

```python
@router.get("/topics/{topic_id}/kcs", response_model=list[KCRead])
async def list_kcs(topic_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    await svc.require_visible_topic(session, topic_id, learner.id)
    return await svc.list_kcs(session, topic_id)


@router.get("/kcs/{kc_id}", response_model=KCDetail)
async def get_kc(kc_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    _, kc = await svc.require_visible_kc(session, kc_id, learner.id)
    edges = await svc.list_prerequisites(session, kc_id)
    # ... rest of the function unchanged ...
```

- [ ] **Step 6: Re-express the notes gates**

In `app/api/v1/notes.py`:

```python
async def _topic_404(session: SessionDep, topic_id: uuid.UUID, learner_id: uuid.UUID) -> Topic:
    _, topic = await knowledge_svc.require_visible_topic(session, topic_id, learner_id)
    return topic
```

and in `notes_index` replace the three-line visibility check with:

```python
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
```

In `app/services/notes.py`, keep `PermissionError` as the exception its callers catch:

```python
async def _require_visible_topic(
    session: AsyncSession, learner_id: uuid.UUID, topic: Topic
) -> None:
    try:
        await knowledge_svc.require_visible_topic(session, topic.id, learner_id)
    except knowledge_svc.NotVisible as exc:
        raise PermissionError("topic not found") from exc
```

Remove any import that becomes unused (`ruff check` reports it).

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_visibility_gate.py tests/test_subject_ownership.py tests/test_notes_api.py tests/test_notes_service.py tests/test_prerequisites.py tests/test_graph_validation.py -q`
Expected: all pass. The existing ownership and notes suites prove the response bodies did not change.

- [ ] **Step 8: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`
Expected: all green.

```bash
git add app/services/knowledge.py app/main.py app/api/v1/knowledge.py app/api/v1/notes.py \
  app/services/notes.py tests/test_visibility_gate.py
git commit -m "refactor(knowledge): one visibility gate for every graph id [S25]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git status --short   # must show only ' M docs/guru-suggestions-tracker.md'
```

---

### Task 2: Cross-learner table; gate lesson plans and mastery

**Files:**
- Create: `tests/test_visibility_sweep.py`
- Modify: `app/api/v1/lesson_plan.py`, `app/api/v1/analytics.py`

**Interfaces:**
- Consumes: `knowledge.require_visible_subject` (Task 1) and the global 404 handler.
- Produces, in `tests/test_visibility_sweep.py`:
  - `Ids`, `World`, `Case` (fields `method`, `path`, `field`, `sends`, `call`, `owner_exempt`);
  - the `CASES: list[Case]` list;
  - the fixtures `world` and `_fakes`, and the helper `_private_graph`.
- Tasks 3 and 4 append to `CASES`; Task 5 reads it.

- [ ] **Step 1: Write the harness and the cases for every already-gated operation, plus lesson plan and mastery**

Create `tests/test_visibility_sweep.py`:

```python
"""Every graph id a request can carry is visibility-checked, through the real API (S25).

Each case calls one operation three ways:

* as learner B with learner A's private ids. The response must equal B's response to random
  ids, byte for byte, once the ids B sent are replaced by a placeholder. No row anywhere may
  change;
* as B with random ids, which is the reference "does not exist" answer;
* as A with A's own ids. The response must differ from the reference, or rows must change.
  Otherwise the fixture never reaches the handler, and a route that 404s for everybody would
  pass.

The drift guard at the bottom fails when an operation accepting one of these ids is not in the
table, so a new route cannot skip the check silently.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_blob_store, get_ingestion_enqueuer, get_llm_client, get_retag_enqueuer
from app.core.db import Base
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.assessment import Item, ItemKC, ItemOrigin, ItemType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.storage import InMemoryBlobStore
from tests.conftest import sign_in

API = "/api/v1"


@dataclass(frozen=True)
class Ids:
    subject: uuid.UUID
    topic: uuid.UUID
    kc: uuid.UUID
    kc2: uuid.UUID  # a second component in the same topic, stored as a prerequisite of `kc`
    item: uuid.UUID


@dataclass(frozen=True)
class World:
    a: Learner
    b: Learner
    a_ids: Ids  # learner A's private graph: the target
    b_ids: Ids  # learner B's own private graph, for routes that take two ids


# (client, target ids, caller's own ids) -> response
Call = Callable[[AsyncClient, Ids, Ids], Awaitable[Response]]


@dataclass(frozen=True)
class Case:
    method: str
    path: str  # the OpenAPI path template, exactly as `app.openapi()` names it
    field: str  # the id field this case targets, as the drift guard names it
    sends: tuple[str, ...]  # the `Ids` attributes this call puts into the request
    call: Call
    owner_exempt: str | None = None  # why the owner's answer cannot differ, when it cannot

    @property
    def key(self) -> str:
        return f"{self.method} {self.path} [{self.field}]"


def _random_ids() -> Ids:
    return Ids(*(uuid.uuid4() for _ in range(5)))


async def _private_graph(session: AsyncSession, owner: Learner) -> Ids:
    tag = uuid.uuid4().hex[:8]
    subject = Subject(slug=f"s-{tag}", name=f"Private {tag}", owner_learner_id=owner.id)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{tag}", name="Topic")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{tag}", name="Component")
    kc2 = KC(topic_id=topic.id, slug=f"p-{tag}", name="Prerequisite")
    session.add_all([kc, kc2])
    await session.flush()
    session.add(KCEdge(prereq_kc_id=kc2.id, kc_id=kc.id))
    item = Item(
        item_type=ItemType.MCQ,
        stem="Private question",
        answer_key={"choices": ["a", "b"], "correct": 0},
        owner_learner_id=owner.id,
        author_learner_id=owner.id,
        origin=ItemOrigin.LEARNER,
        kc_links=[ItemKC(kc_id=kc.id)],
    )
    session.add(item)
    await session.flush()
    return Ids(subject=subject.id, topic=topic.id, kc=kc.id, kc2=kc2.id, item=item.id)


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, api_learner: Learner) -> World:
    b = Learner(handle=f"b-{uuid.uuid4().hex[:8]}")
    db_session.add(b)
    await db_session.flush()
    a_ids = await _private_graph(db_session, api_learner)
    b_ids = await _private_graph(db_session, b)
    await db_session.commit()
    return World(a=api_learner, b=b, a_ids=a_ids, b_ids=b_ids)


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[None]:
    """Deterministic stand-ins for every paid or external dependency an owner call can reach."""
    store = InMemoryBlobStore()

    async def _nothing(_id: uuid.UUID) -> None:
        return None

    reply = json.dumps({"body": "A private explanation.", "citations": []})
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(reply=reply)
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: _nothing
    app.dependency_overrides[get_retag_enqueuer] = lambda: _nothing
    yield
    for dep in (get_llm_client, get_blob_store, get_ingestion_enqueuer, get_retag_enqueuer):
        app.dependency_overrides.pop(dep, None)


async def _row_counts(session: AsyncSession) -> dict[str, int]:
    """Every table's row count, in one round trip."""
    stmt = union_all(
        *(
            select(literal(table.name).label("t"), func.count().label("n")).select_from(table)
            for table in Base.metadata.sorted_tables
        )
    )
    return {name: count for name, count in (await session.execute(stmt)).all()}


def _normalised(r: Response, ids: Ids, sends: tuple[str, ...]) -> tuple[int, bytes]:
    """Status and body, with only the ids this call sent replaced by a placeholder.

    Only the *sent* ids are replaced. An id the caller never sent (a stranger's subject named in
    an error) stays verbatim and makes the comparison fail, which is the point.
    """
    body = r.content
    for attr in sends:
        body = body.replace(str(getattr(ids, attr)).encode(), b"<id>")
    return r.status_code, body


CASES: list[Case] = [
    # --- knowledge graph -----------------------------------------------------------------
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}"),
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/coverage",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/coverage"),
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/cross-subject-prerequisites",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/cross-subject-prerequisites"),
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/prerequisite-conflicts",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/prerequisite-conflicts"),
    ),
    Case(
        "POST",
        "/api/v1/subjects/{subject_id}/topics",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(
            f"{API}/subjects/{t.subject}/topics", json={"slug": "new-topic", "name": "New topic"}
        ),
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/topics",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/topics"),
    ),
    Case(
        "POST",
        "/api/v1/topics/{topic_id}/kcs",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.post(
            f"{API}/topics/{t.topic}/kcs", json={"slug": "new-kc", "name": "New component"}
        ),
    ),
    Case(
        "GET",
        "/api/v1/topics/{topic_id}/kcs",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.get(f"{API}/topics/{t.topic}/kcs"),
    ),
    Case(
        "GET",
        "/api/v1/kcs/{kc_id}",
        "kc_id",
        ("kc",),
        lambda c, t, o: c.get(f"{API}/kcs/{t.kc}"),
    ),
    Case(
        "POST",
        "/api/v1/kcs/{kc_id}/prerequisites",
        "kc_id",
        ("kc",),
        lambda c, t, o: c.post(
            f"{API}/kcs/{t.kc}/prerequisites", json={"prereq_kc_id": str(o.kc2)}
        ),
    ),
    Case(
        "POST",
        "/api/v1/kcs/{kc_id}/prerequisites",
        "prereq_kc_id",
        ("kc2",),
        lambda c, t, o: c.post(
            f"{API}/kcs/{o.kc}/prerequisites", json={"prereq_kc_id": str(t.kc2)}
        ),
    ),
    Case(
        "DELETE",
        "/api/v1/kcs/{kc_id}/prerequisites/{prereq_kc_id}",
        "kc_id",
        ("kc",),
        lambda c, t, o: c.delete(f"{API}/kcs/{t.kc}/prerequisites/{o.kc2}"),
    ),
    Case(
        "DELETE",
        "/api/v1/kcs/{kc_id}/prerequisites/{prereq_kc_id}",
        "prereq_kc_id",
        ("kc2",),
        lambda c, t, o: c.delete(f"{API}/kcs/{o.kc}/prerequisites/{t.kc2}"),
    ),
    # --- assessment ----------------------------------------------------------------------
    Case(
        "POST",
        "/api/v1/items",
        "kcs[].kc_id",
        ("kc",),
        lambda c, t, o: c.post(
            f"{API}/items",
            json={
                "item_type": "mcq",
                "stem": "Q",
                "answer_key": {"choices": ["a", "b"], "correct": 0},
                "kcs": [{"kc_id": str(t.kc)}],
            },
        ),
    ),
    Case(
        "GET",
        "/api/v1/items/{item_id}",
        "item_id",
        ("item",),
        lambda c, t, o: c.get(f"{API}/items/{t.item}"),
    ),
    Case(
        "POST",
        "/api/v1/items/{item_id}/answer",
        "item_id",
        ("item",),
        lambda c, t, o: c.post(f"{API}/items/{t.item}/answer", json={"response": {"choice": 0}}),
    ),
    # --- placement and conversations -----------------------------------------------------
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/placement/prompt",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/placement/prompt"),
    ),
    Case(
        "POST",
        "/api/v1/subjects/{subject_id}/placement",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(
            f"{API}/subjects/{t.subject}/placement", json={"background": "Studied it years ago."}
        ),
    ),
    Case(
        "POST",
        "/api/v1/conversations",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(f"{API}/conversations", json={"subject_id": str(t.subject)}),
    ),
    # --- notes ---------------------------------------------------------------------------
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/notes",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/notes"),
    ),
    Case(
        "GET",
        "/api/v1/topics/{topic_id}/note",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.get(f"{API}/topics/{t.topic}/note"),
    ),
    Case(
        "PUT",
        "/api/v1/topics/{topic_id}/note",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.put(f"{API}/topics/{t.topic}/note", json={"content_md": "Mine."}),
    ),
    Case(
        "PATCH",
        "/api/v1/topics/{topic_id}/note/format",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.patch(f"{API}/topics/{t.topic}/note/format", json={"format": None}),
    ),
    Case(
        "POST",
        "/api/v1/topics/{topic_id}/note/refresh",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.post(f"{API}/topics/{t.topic}/note/refresh"),
    ),
    Case(
        "GET",
        "/api/v1/topics/{topic_id}/note/revisions",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.get(f"{API}/topics/{t.topic}/note/revisions"),
    ),
    Case(
        "GET",
        "/api/v1/topics/{topic_id}/note/revisions/{ordinal}",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.get(f"{API}/topics/{t.topic}/note/revisions/1"),
    ),
    Case(
        "POST",
        "/api/v1/topics/{topic_id}/note/revisions/{ordinal}/restore",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.post(
            f"{API}/topics/{t.topic}/note/revisions/1/restore",
            json={"expected_revision_ordinal": 1},
        ),
    ),
    # --- lesson plans and analytics ------------------------------------------------------
    Case(
        "POST",
        "/api/v1/subjects/{subject_id}/lesson-plan",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(f"{API}/subjects/{t.subject}/lesson-plan", json={"goal": None}),
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/lesson-plan",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/lesson-plan"),
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/mastery",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/mastery"),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
async def test_a_strangers_graph_id_is_answered_as_if_it_did_not_exist(
    case: Case, world: World, api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await sign_in(api_client, db_session, world.b)
    before = await _row_counts(db_session)
    foreign = await case.call(api_client, world.a_ids, world.b_ids)
    after = await _row_counts(db_session)
    changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    assert not changed, f"{case.key}: a stranger's id changed rows {changed}"

    random_ids = _random_ids()
    reference = await case.call(api_client, random_ids, world.b_ids)
    assert _normalised(foreign, world.a_ids, case.sends) == _normalised(
        reference, random_ids, case.sends
    ), f"{case.key}: a stranger's id is distinguishable from a missing one"

    if case.owner_exempt is not None:
        return
    await sign_in(api_client, db_session, world.a)
    owner_before = await _row_counts(db_session)
    owner = await case.call(api_client, world.a_ids, world.a_ids)
    owner_after = await _row_counts(db_session)
    reached = owner_after != owner_before or _normalised(
        owner, world.a_ids, case.sends
    ) != _normalised(reference, random_ids, case.sends)
    assert reached, f"{case.key}: the owner got the not-found answer too; the case never runs"
```

- [ ] **Step 2: Run it and confirm exactly the expected failures**

Run: `uv run pytest tests/test_visibility_sweep.py -q`

Expected: FAIL for exactly these three cases.
- `POST .../lesson-plan`: rows changed (a plan was created).
- `GET .../lesson-plan`: "no lesson plan for this subject yet" vs "subject not found".
- `GET .../mastery`: 200 vs 404.

All other cases PASS.

If another case fails, determine why before changing anything:

- **The owner assertion fails** because the owner lacks a precondition: e.g. a revision route
  answering exactly like a missing topic. Add the precondition to `_private_graph`, with a comment
  saying which case needs it. Do not weaken the assertion.
- **An owner call raises inside the app** (the test client re-raises app exceptions). Adjust that
  case's request body, or the `_fakes` reply, until the owner call is a real request. Record the
  reason in a comment.
- **A foreign call is distinguishable, or changes rows**, on an operation this plan calls
  already-gated. That is a real finding: **stop and report it** with the case key and the
  observed responses; do not fix it inside this task.

- [ ] **Step 3: Gate the lesson-plan and mastery routes**

`app/api/v1/lesson_plan.py`: in both handlers, replace the two-line `get_subject(...) is None`
check with

```python
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
```

The GET keeps its `"no lesson plan for this subject yet"` 404, now reachable only after the gate.

`app/api/v1/analytics.py` (`get_subject_mastery`): the same replacement. Then remove the
`HTTPException, status` import if nothing else in the file uses it.

- [ ] **Step 4: Run the table**

Run: `uv run pytest tests/test_visibility_sweep.py tests/test_lesson_plan.py tests/test_analytics.py -q`
Expected: all pass.

- [ ] **Step 5: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

```bash
git add tests/test_visibility_sweep.py app/api/v1/lesson_plan.py app/api/v1/analytics.py
git commit -m "fix(api): refuse lesson plans and mastery over a stranger's subject [S25]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git status --short   # must show only ' M docs/guru-suggestions-tracker.md'
```

---

### Task 3: Gate content generation and cached content

**Files:**
- Modify: `app/api/v1/content.py`
- Modify: `tests/test_visibility_sweep.py` (append two cases to `CASES`)

**Interfaces:**
- Consumes: `knowledge.require_visible_kc` (Task 1); `CASES` and `Case` (Task 2).

- [ ] **Step 1: Append the content cases**

Add to the end of `CASES` in `tests/test_visibility_sweep.py`, under a `# --- content ---` rule:

```python
    Case(
        "POST",
        "/api/v1/content/generate",
        "kc_id",
        ("kc",),
        lambda c, t, o: c.post(f"{API}/content/generate", json={"kc_id": str(t.kc)}),
    ),
    Case(
        "GET",
        "/api/v1/content/kc/{kc_id}",
        "kc_id",
        ("kc",),
        lambda c, t, o: c.get(f"{API}/content/kc/{t.kc}"),
    ),
```

- [ ] **Step 2: Run and confirm the failures**

Run: `uv run pytest tests/test_visibility_sweep.py -q -k content`

Expected: both fail.
- `POST .../generate`, foreign: rows changed (`content_blocks` and/or `llm_calls`), or the random
  call raises `LookupError` out of the app.
- `GET .../kc/{kc_id}`: the owner assertion fails. Everybody gets `200 []`.

- [ ] **Step 3: Gate both routes**

In `app/api/v1/content.py` add `from app.services import knowledge as knowledge_svc`, then make
the first statement of `generate_content`

```python
    await knowledge_svc.require_visible_kc(session, data.kc_id, learner.id)
```

and the first statement of `get_kc_content`

```python
    await knowledge_svc.require_visible_kc(session, kc_id, learner.id)
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_visibility_sweep.py tests/test_content.py -q`
Expected: all pass. If a `test_content.py` case generated content for a KC that does not exist,
it was relying on the bug. Update it to use a real, visible KC, and say so in the commit body.

- [ ] **Step 5: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

```bash
git add app/api/v1/content.py tests/test_visibility_sweep.py
git commit -m "fix(content): refuse generation and reads for a stranger's component [S25]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git status --short   # must show only ' M docs/guru-suggestions-tracker.md'
```

---

### Task 4: Gate source scope, retrieval and source listing

**Files:**
- Modify: `app/services/knowledge.py` (`resolve_source_scope`, around line 334)
- Modify: `app/services/ingestion.py:95` and `:195` (both `resolve_source_scope` calls)
- Modify: `app/api/v1/sources.py` (`list_sources`, `retrieve_chunks`)
- Modify: `tests/test_source_scope.py`
- Modify: `tests/test_visibility_sweep.py` (append cases and two tests)

**Interfaces:**
- Consumes: the three resolvers and `NotVisible` (Task 1); `CASES`, `Case`, `World`, `world` (Task 2).
- Produces: `async def resolve_source_scope(session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID | None, topic_id: uuid.UUID | None) -> tuple[uuid.UUID | None, uuid.UUID | None]`.
  The keyword `learner_id` is now required.

- [ ] **Step 1: Append the source cases and the scrubbed-message tests**

Append to `CASES`, under a `# --- sources and retrieval ---` rule:

```python
    Case(
        "POST",
        "/api/v1/sources/upload",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(
            f"{API}/sources/upload",
            files={"file": ("notes.txt", b"private notes", "text/plain")},
            data={"subject_id": str(t.subject)},
        ),
    ),
    Case(
        "POST",
        "/api/v1/sources/upload",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.post(
            f"{API}/sources/upload",
            files={"file": ("notes.txt", b"private notes", "text/plain")},
            data={"topic_id": str(t.topic)},
        ),
    ),
    Case(
        "POST",
        "/api/v1/sources/link",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(
            f"{API}/sources/link",
            json={"url": "https://example.com/a", "subject_id": str(t.subject)},
        ),
        owner_exempt="every caller gets the same 403: URL intake is disabled in v0 (V06)",
    ),
    Case(
        "POST",
        "/api/v1/sources/link",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.post(
            f"{API}/sources/link", json={"url": "https://example.com/a", "topic_id": str(t.topic)}
        ),
        owner_exempt="every caller gets the same 403: URL intake is disabled in v0 (V06)",
    ),
    Case(
        "GET",
        "/api/v1/sources",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/sources", params={"subject_id": str(t.subject)}),
    ),
    Case(
        "POST",
        "/api/v1/retrieve",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(
            f"{API}/retrieve", json={"query": "anything", "subject_id": str(t.subject)}
        ),
    ),
    Case(
        "POST",
        "/api/v1/retrieve",
        "topic_id",
        ("topic",),
        lambda c, t, o: c.post(
            f"{API}/retrieve", json={"query": "anything", "topic_id": str(t.topic)}
        ),
    ),
```

Append these two tests at the end of the file:

```python
async def test_a_scope_error_never_names_a_strangers_subject(
    world: World, api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The old message named the subject a topic belongs to, which let anybody map a stranger's
    topic to their subject by uploading against it."""
    await sign_in(api_client, db_session, world.b)

    r = await api_client.post(
        f"{API}/sources/upload",
        files={"file": ("notes.txt", b"private notes", "text/plain")},
        data={"subject_id": str(world.b_ids.subject), "topic_id": str(world.a_ids.topic)},
    )

    assert r.status_code == 422
    assert str(world.a_ids.subject) not in r.text


async def test_a_mismatched_scope_names_only_what_was_sent(
    world: World, api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Both ids visible, but the topic is not in the subject: the message may name both, and
    nothing else."""
    tag = uuid.uuid4().hex[:8]
    curated = Subject(slug=f"c-{tag}", name=f"Curated {tag}", owner_learner_id=None)
    db_session.add(curated)
    await db_session.flush()
    curated_topic = Topic(subject_id=curated.id, slug=f"ct-{tag}", name="Curated topic")
    db_session.add(curated_topic)
    await db_session.commit()
    await sign_in(api_client, db_session, world.b)

    r = await api_client.post(
        f"{API}/sources/upload",
        files={"file": ("notes.txt", b"private notes", "text/plain")},
        data={"subject_id": str(world.b_ids.subject), "topic_id": str(curated_topic.id)},
    )

    assert r.status_code == 422
    assert r.json() == {
        "detail": f"topic {curated_topic.id} does not belong to subject {world.b_ids.subject}"
    }
    assert str(curated.id) not in r.text
```

- [ ] **Step 2: Run and confirm the failures**

Run: `uv run pytest tests/test_visibility_sweep.py -q -k "sources or retrieve or scope"`

Expected failures:
- Both `upload` cases: rows changed. A `sources` row appears for a stranger's subject.
- `GET /api/v1/sources` and both `retrieve` cases: the owner assertion fails. Everybody gets
  `200 []`.
- `test_a_scope_error_never_names_a_strangers_subject`: the message contains the stranger's
  subject id.
- `test_a_mismatched_scope_names_only_what_was_sent`: the old wording is `"belongs to subject"`.

Both `link` cases pass: the route is a constant 403.

- [ ] **Step 3: Make scope resolution learner-aware and scrub its messages**

Replace `resolve_source_scope` in `app/services/knowledge.py`. Keep its docstring, and add the
paragraph shown:

```python
async def resolve_source_scope(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    topic_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(existing docstring, plus:)

    Both ids pass the visibility gate first (S25). A stranger's private subject or topic is
    refused exactly like one that does not exist. Every message names only ids the caller sent:
    the old one named the subject a topic belongs to, which told anybody who uploaded against a
    stranger's topic whose curriculum it was.
    """
    if subject_id is not None:
        try:
            await require_visible_subject(session, subject_id, learner_id)
        except NotVisible as exc:
            raise ScopeConflict(f"subject {subject_id} does not exist") from exc
    if topic_id is None:
        return subject_id, None
    try:
        _, topic = await require_visible_topic(session, topic_id, learner_id)
    except NotVisible as exc:
        raise ScopeConflict(f"topic {topic_id} does not exist") from exc
    if subject_id is not None and topic.subject_id != subject_id:
        raise ScopeConflict(f"topic {topic_id} does not belong to subject {subject_id}")
    return topic.subject_id, topic_id
```

`resolve_source_scope` is defined above the resolvers in the file. That is fine: they are only
looked up when it is called.

In `app/services/ingestion.py`, pass the learner in both calls (in `create_source` and
`create_or_reuse_source`):

```python
    subject_id, topic_id = await knowledge.resolve_source_scope(
        session, learner_id=learner_id, subject_id=subject_id, topic_id=topic_id
    )
```

- [ ] **Step 4: Gate listing and retrieval in `app/api/v1/sources.py`**

In `list_sources`, before building the statement:

```python
    if subject_id is not None:
        await knowledge.require_visible_subject(session, subject_id, learner.id)
```

In `retrieve_chunks`, before calling `retrieval.retrieve`:

```python
    if data.subject_id is not None:
        await knowledge.require_visible_subject(session, data.subject_id, learner.id)
    if data.topic_id is not None:
        await knowledge.require_visible_topic(session, data.topic_id, learner.id)
```

- [ ] **Step 5: Update `tests/test_source_scope.py` for the new signature and wording**

Every direct `svc.resolve_source_scope(db_session, ...)` call gains `learner_id=`. Use a learner
from the file's `_learner` helper: its subjects are built by `_graph`; if `_graph` makes them owned,
use that owner, and if they are curated (`owner_learner_id=None`), any learner works. Change the
assertion at line 106 from `assert "belongs to subject" in r.text` to
`assert "does not belong to subject" in r.text`. Do not change what any test proves.

- [ ] **Step 6: Run**

Run: `uv run pytest tests/test_visibility_sweep.py tests/test_source_scope.py tests/test_sources_api.py tests/test_retrieval.py tests/test_ingestion_jobs.py -q`
Expected: all pass. If a retrieve owner call raises inside the app (e.g. over the embedding
space), fix the case's setup, not the assertion, and note why.

- [ ] **Step 7: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

```bash
git add app/services/knowledge.py app/services/ingestion.py app/api/v1/sources.py \
  tests/test_source_scope.py tests/test_visibility_sweep.py
git commit -m "fix(sources): scope, retrieval and listing refuse a stranger's graph ids [S25]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git status --short   # must show only ' M docs/guru-suggestions-tracker.md'
```

---

### Task 5: Drift guard over the OpenAPI document

**Files:**
- Modify: `tests/test_visibility_sweep.py` (append)

**Interfaces:**
- Consumes: `CASES` and `Case.method/path/field` (Tasks 2–4); `app.openapi()`.

- [ ] **Step 1: Append the guard**

```python
_ID_FIELDS = frozenset({"subject_id", "topic_id", "kc_id", "prereq_kc_id", "item_id"})


def _fields(
    schema: dict, schemas: dict, seen: frozenset[str] = frozenset(), prefix: str = ""
) -> set[str]:
    """Every property path in a JSON schema: `kcs[].kc_id` for a field inside an array."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return set()
        return _fields(schemas[name], schemas, seen | {name}, prefix)
    found: set[str] = set()
    for key in ("anyOf", "allOf", "oneOf"):
        for sub in schema.get(key, []):
            found |= _fields(sub, schemas, seen, prefix)
    if "items" in schema:
        found |= _fields(schema["items"], schemas, seen, prefix + "[]")
    for name, sub in schema.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        found.add(path)
        found |= _fields(sub, schemas, seen, path)
    return found


def _graph_id_operations() -> set[tuple[str, str, str]]:
    """(METHOD, path, field) for every operation accepting a graph or item id, anywhere."""
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    found: set[tuple[str, str, str]] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            names = {p["name"] for p in operation.get("parameters", [])}
            for content in operation.get("requestBody", {}).get("content", {}).values():
                names |= _fields(content.get("schema", {}), schemas)
            for name in names:
                if name.rsplit(".", 1)[-1] in _ID_FIELDS:
                    found.add((method.upper(), path, name))
    return found


def test_every_operation_taking_a_graph_id_is_in_the_table() -> None:
    covered = {(case.method, case.path, case.field) for case in CASES}
    operations = _graph_id_operations()

    missing = sorted(operations - covered)
    assert not missing, (
        "operations accepting a graph id with no cross-learner case (add one to CASES): "
        f"{missing}"
    )
    stale = sorted(covered - operations)
    assert not stale, f"cases naming operations that no longer exist: {stale}"


def test_the_guard_sees_ids_nested_in_request_bodies() -> None:
    """The guard is only as good as its walk. `POST /items` carries its ids inside a list of
    objects, so this pins that the walk reaches them."""
    assert ("POST", "/api/v1/items", "kcs[].kc_id") in _graph_id_operations()
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_visibility_sweep.py -q`
Expected: all pass. If `missing` lists an operation, it is a route this plan's audit did not see.
Add its case: it must satisfy the same three-way test. If the route turns out to be ungated,
**stop and report**, with the operation and the responses; do not gate it silently here.

- [ ] **Step 3: Prove the guard can fail**

Temporarily delete the `GET /api/v1/subjects/{subject_id}/mastery` case from `CASES` and run
`uv run pytest tests/test_visibility_sweep.py -q -k every_operation`. Expected: FAIL naming
`('GET', '/api/v1/subjects/{subject_id}/mastery', 'subject_id')`. Restore the case. Confirm
`git diff tests/test_visibility_sweep.py` shows only the appended guard.

- [ ] **Step 4: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract`

```bash
git add tests/test_visibility_sweep.py
git commit -m "test: fail when an operation taking a graph id escapes the visibility table [S25]

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git status --short   # must show only ' M docs/guru-suggestions-tracker.md'
```
