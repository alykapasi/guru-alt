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
from app.models.publication import CurriculumProposal, Publication, PublicationStatus
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
    proposal: uuid.UUID  # a curriculum-generation record (S25b); `/subjects/commit` needs one
    publication: uuid.UUID  # a pending publication request on `subject` (S25b)


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
    refusal: int = 404  # the status a stranger's (or missing) id gets refused with

    @property
    def key(self) -> str:
        return f"{self.method} {self.path} [{self.field}]"


def _random_ids() -> Ids:
    return Ids(*(uuid.uuid4() for _ in range(7)))


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
    # `/subjects/commit` will not accept another learner's proposal id, so the sweep needs one
    # per learner to have anything to swap (S25b D4).
    proposal = CurriculumProposal(learner_id=owner.id, grounded_in_sources=False)
    session.add(proposal)
    # A pending request, so `/publications/{id}/cancel` has one of each learner's to swap.
    publication = Publication(
        source_subject_id=subject.id,
        author_id=owner.id,
        author_handle=owner.handle,
        status=PublicationStatus.PENDING,
        snapshot={},
    )
    session.add(publication)
    await session.flush()
    return Ids(
        subject=subject.id,
        topic=topic.id,
        kc=kc.id,
        kc2=kc2.id,
        item=item.id,
        proposal=proposal.id,
        publication=publication.id,
    )


@pytest_asyncio.fixture
async def world(db_session: AsyncSession, api_learner: Learner) -> World:
    b = Learner(handle=f"b-{uuid.uuid4().hex[:8]}")
    db_session.add(b)
    await db_session.flush()
    a_ids = await _private_graph(db_session, api_learner)
    b_ids = await _private_graph(db_session, b)
    await db_session.commit()
    return World(a=api_learner, b=b, a_ids=a_ids, b_ids=b_ids)


@dataclass
class Enqueued:
    """What each enqueuer stub was actually asked to enqueue, so a test can prove "nothing"."""

    ingestion: list[uuid.UUID]
    retag: list[uuid.UUID]


@pytest.fixture(autouse=True)
def _fakes() -> Iterator[Enqueued]:
    """Deterministic stand-ins for every paid or external dependency an owner call can reach."""
    store = InMemoryBlobStore()
    enqueued = Enqueued(ingestion=[], retag=[])

    async def _record_ingestion(id_: uuid.UUID) -> None:
        enqueued.ingestion.append(id_)

    async def _record_retag(id_: uuid.UUID) -> None:
        enqueued.retag.append(id_)

    reply = json.dumps({"body": "A private explanation.", "citations": []})
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(reply=reply)
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: _record_ingestion
    app.dependency_overrides[get_retag_enqueuer] = lambda: _record_retag
    yield enqueued
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
    # --- publication review (administrator routes) --------------------------------------
    # An ordinary learner gets 403 on all of these whatever id they send, so the property
    # worth pinning is that the refusal is *identical* for a real request and an invented
    # one. Allowlisting `publication_id` instead would have exempted the learner-facing
    # cancel route above from the guard the day somebody added another one.
    Case(
        "GET",
        "/api/v1/admin/publications/{publication_id}",
        "publication_id",
        ("publication",),
        lambda c, t, o: c.get(f"{API}/admin/publications/{t.publication}"),
        owner_exempt="every caller without the admin tier gets the same 403, whether the request exists or not — these are review routes, not learner routes",
        refusal=403,
    ),
    Case(
        "POST",
        "/api/v1/admin/publications/{publication_id}/approve",
        "publication_id",
        ("publication",),
        lambda c, t, o: c.post(f"{API}/admin/publications/{t.publication}/approve", json={}),
        owner_exempt="every caller without the admin tier gets the same 403, whether the request exists or not — these are review routes, not learner routes",
        refusal=403,
    ),
    Case(
        "POST",
        "/api/v1/admin/publications/{publication_id}/approve",
        "excluded_item_ids[]",
        ("item",),
        lambda c, t, o: c.post(
            f"{API}/admin/publications/{o.publication}/approve",
            json={"excluded_item_ids": [str(t.item)]},
        ),
        owner_exempt="every caller without the admin tier gets the same 403, whether the request exists or not — these are review routes, not learner routes",
        refusal=403,
    ),
    Case(
        "POST",
        "/api/v1/admin/publications/{publication_id}/reject",
        "publication_id",
        ("publication",),
        lambda c, t, o: c.post(
            f"{API}/admin/publications/{t.publication}/reject",
            json={"note": "a note long enough to pass"},
        ),
        owner_exempt="every caller without the admin tier gets the same 403, whether the request exists or not — these are review routes, not learner routes",
        refusal=403,
    ),
    Case(
        "POST",
        "/api/v1/admin/subjects/{subject_id}/withdraw",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(
            f"{API}/admin/subjects/{t.subject}/withdraw", json={"reason": "a stated reason"}
        ),
        owner_exempt="every caller without the admin tier gets the same 403, whether the request exists or not — these are review routes, not learner routes",
        refusal=403,
    ),
    Case(
        "POST",
        "/api/v1/subjects/{subject_id}/publications",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.post(f"{API}/subjects/{t.subject}/publications", json={}),
        # The owner already has a pending request from the fixture, so asking again is a 409 —
        # different from the 404 a stranger gets, which is all this case needs to show.
    ),
    Case(
        "GET",
        "/api/v1/subjects/{subject_id}/publications",
        "subject_id",
        ("subject",),
        lambda c, t, o: c.get(f"{API}/subjects/{t.subject}/publications"),
    ),
    Case(
        "POST",
        "/api/v1/publications/{publication_id}/cancel",
        "publication_id",
        ("publication",),
        # Not a graph id either, but the same rule applies: whose request it is must not be
        # answerable, or publication activity becomes enumerable one id at a time (S25b).
        lambda c, t, o: c.post(f"{API}/publications/{t.publication}/cancel"),
    ),
    Case(
        "POST",
        "/api/v1/subjects/commit",
        "proposal_id",
        ("proposal",),
        # Not a graph id, but it behaves exactly like one: it names a row belonging to one
        # learner, and a caller who could tell "somebody else's" from "no such thing" could
        # probe for other learners' activity (S25b D4).
        lambda c, t, o: c.post(
            f"{API}/subjects/commit",
            json={
                "subject_name": f"Swept {uuid.uuid4().hex[:8]}",
                "subject_description": None,
                "topics": [],
                "source_ids": None,
                "proposal_id": str(t.proposal),
            },
        ),
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
        # deleting an edge is idempotent: a missing (or foreign) prereq_kc_id with a real,
        # writable kc_id is a no-op success, not a refusal (see `remove_prerequisite`'s docstring)
        refusal=204,
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
        refusal=422,
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
        refusal=422,
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
        refusal=403,
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
        refusal=403,
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
    case: Case, world: World, api_client: AsyncClient, db_session: AsyncSession, _fakes: Enqueued
) -> None:
    await sign_in(api_client, db_session, world.b)
    before = await _row_counts(db_session)
    enqueued_before = (list(_fakes.ingestion), list(_fakes.retag))
    foreign = await case.call(api_client, world.a_ids, world.b_ids)
    after = await _row_counts(db_session)
    changed = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
    assert not changed, f"{case.key}: a stranger's id changed rows {changed}"
    assert (_fakes.ingestion, _fakes.retag) == enqueued_before, (
        f"{case.key}: a stranger's id enqueued work"
    )

    random_ids = _random_ids()
    reference = await case.call(api_client, random_ids, world.b_ids)
    assert reference.status_code == case.refusal, (
        f"{case.key}: expected a missing id to be refused with {case.refusal}, "
        f"got {reference.status_code}"
    )
    assert _normalised(foreign, world.a_ids, case.sends) == _normalised(
        reference, random_ids, case.sends
    ), f"{case.key}: a stranger's id is distinguishable from a missing one"

    await sign_in(api_client, db_session, world.a)
    owner_before = await _row_counts(db_session)
    owner = await case.call(api_client, world.a_ids, world.a_ids)
    owner_after = await _row_counts(db_session)

    if case.owner_exempt is not None:
        assert _normalised(owner, world.a_ids, case.sends) == _normalised(
            reference, random_ids, case.sends
        ), (
            f"{case.key}: owner_exempt says {case.owner_exempt!r}, but the owner's answer now "
            "differs from the reference — the exemption has expired and must be removed"
        )
        return
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


_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "options", "head", "trace"})

# Every uuid-typed input in the live API that is not a subject/topic/kc/item id, confirmed
# against `app.openapi()` (see the F1 fix report for how each was checked). One line each for
# what it actually names — a new uuid input must be added here, or to `CASES`, or the guard
# fails naming it.
_NOT_GRAPH_IDS = frozenset(
    {
        "learner_id",  # target learner: /admin/impersonate, /admin/learners/{id}/suspend|reinstate
        "impersonation_id",  # an admin impersonation session id
        "invitation_id",  # an admin invitation row id
        "source_id",  # a Source row id
        "source_ids",  # Source row ids reassigned between subjects (conversations, commit, ...)
        "conversation_id",  # a chat conversation id
        "before",  # a message id used as a pagination cursor
        "client_turn_id",  # idempotency key for a chat turn
        "rubric_id",  # an item's optional rubric reference, not a graph id
        "attempt_id",  # idempotency key for an answer submission
        "chunk_id",  # a source chunk id
        "block_id",  # a content block id
        "memory_id",  # a learner memory row id
    }
)


def _resolve(schema: dict, schemas: dict, seen: frozenset[str]) -> Iterator[dict]:
    """Every concrete schema reachable from `schema` via `$ref`/`anyOf`/`allOf`/`oneOf`."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return
        yield from _resolve(schemas[name], schemas, seen | {name})
        return
    for key in ("anyOf", "allOf", "oneOf"):
        for sub in schema.get(key, []):
            yield from _resolve(sub, schemas, seen)
    yield schema


def _is_uuid(schema: dict, schemas: dict) -> bool:
    return any(
        v.get("type") == "string" and v.get("format") == "uuid"
        for v in _resolve(schema, schemas, frozenset())
    )


def _uuid_fields(
    schema: dict, schemas: dict, seen: frozenset[str] = frozenset(), prefix: str = ""
) -> set[str]:
    """Every property path whose schema is `type: string, format: uuid`.

    Classifies by type, not name, so a renamed field cannot escape the guard. Walks `$ref`,
    `items` (marking `[]`), `anyOf`/`allOf`/`oneOf`, and dict values (`additionalProperties`,
    marking `{}`) — `kcs[].kc_id` for a field inside an array, `topics{}.kc_id` for one inside a
    dict's values.
    """
    found: set[str] = set()
    for variant in _resolve(schema, schemas, seen):
        if prefix and _is_uuid(variant, schemas):
            found.add(prefix)
        if "items" in variant:
            found |= _uuid_fields(variant["items"], schemas, seen, prefix + "[]")
        additional = variant.get("additionalProperties")
        if isinstance(additional, dict):
            found |= _uuid_fields(additional, schemas, seen, prefix + "{}")
        for name, sub in variant.get("properties", {}).items():
            path = f"{prefix}.{name}" if prefix else name
            found |= _uuid_fields(sub, schemas, seen, path)
    return found


def _leaf_name(field: str) -> str:
    """The plain field name a path like `kcs[].kc_id` or `source_ids[]` ends in."""
    trimmed = field
    while trimmed.endswith("[]") or trimmed.endswith("{}"):
        trimmed = trimmed[:-2]
    return trimmed.rsplit(".", 1)[-1]


def _graph_id_operations(spec: dict) -> set[tuple[str, str, str]]:
    """(METHOD, path, field) for every operation accepting a uuid-typed input, anywhere: a path,
    query or header param, or anything nested in a request body — regardless of its name."""
    schemas = spec.get("components", {}).get("schemas", {})
    found: set[tuple[str, str, str]] = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in _HTTP_METHODS:
                continue
            for param in operation.get("parameters", []):
                param_schema = param.get("schema", {})
                fields = _uuid_fields(param_schema, schemas, prefix=param["name"])
                if _is_uuid(param_schema, schemas):
                    fields.add(param["name"])
                found |= {(method.upper(), path, field) for field in fields}
            for content in operation.get("requestBody", {}).get("content", {}).values():
                fields = _uuid_fields(content.get("schema", {}), schemas)
                found |= {(method.upper(), path, field) for field in fields}
    return found


def _uncovered(
    operations: set[tuple[str, str, str]], covered: set[tuple[str, str, str]]
) -> set[tuple[str, str, str]]:
    """Uuid-typed inputs with neither a `CASES` entry nor an allowlisted leaf name."""
    return {
        (method, path, field)
        for method, path, field in operations - covered
        if _leaf_name(field) not in _NOT_GRAPH_IDS
    }


def test_every_operation_taking_a_graph_id_is_in_the_table() -> None:
    covered = {(case.method, case.path, case.field) for case in CASES}
    operations = _graph_id_operations(app.openapi())

    missing = sorted(_uncovered(operations, covered))
    assert not missing, (
        "operations accepting a uuid with no cross-learner case and no _NOT_GRAPH_IDS entry "
        f"(add one to CASES, or a commented allowlist entry if it truly is not a graph id): "
        f"{missing}"
    )
    stale = sorted(covered - operations)
    assert not stale, f"cases naming operations that no longer exist: {stale}"


def test_the_guard_sees_ids_nested_in_request_bodies() -> None:
    """The guard is only as good as its walk. `POST /items` carries its ids inside a list of
    objects, so this pins that the walk reaches them."""
    assert ("POST", "/api/v1/items", "kcs[].kc_id") in _graph_id_operations(app.openapi())


def test_the_guard_classifies_by_type_not_name() -> None:
    """Type-based classification must catch what name matching would miss: a renamed array
    field, an id nested inside a dict's values, and an id moved into a query parameter. Fed a
    hand-written OpenAPI fragment, not the real app, so this cannot pass by accident of what
    routes happen to exist today — and does not require adding routes to the real app to prove
    the walk works."""
    fragment = {
        "paths": {
            "/probe/renamed-array": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "kc_ids": {
                                            "type": "array",
                                            "items": {"type": "string", "format": "uuid"},
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {},
                }
            },
            "/probe/dict-value": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "topics": {
                                            "type": "object",
                                            "additionalProperties": {
                                                "$ref": "#/components/schemas/Probe"
                                            },
                                        }
                                    },
                                }
                            }
                        }
                    },
                    "responses": {},
                }
            },
            "/probe/query-param": {
                "get": {
                    "parameters": [
                        {
                            "name": "focus_topic",
                            "in": "query",
                            "schema": {"type": "string", "format": "uuid"},
                        }
                    ],
                    "responses": {},
                }
            },
        },
        "components": {
            "schemas": {
                "Probe": {
                    "type": "object",
                    "properties": {"kc_id": {"type": "string", "format": "uuid"}},
                }
            }
        },
    }

    missing = _uncovered(_graph_id_operations(fragment), covered=set())

    assert missing == {
        ("POST", "/probe/renamed-array", "kc_ids[]"),
        ("POST", "/probe/dict-value", "topics{}.kc_id"),
        ("GET", "/probe/query-param", "focus_topic"),
    }
