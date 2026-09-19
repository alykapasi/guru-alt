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
    # --- content ---
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
    # --- sources and retrieval -------------------------------------------------------------
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
