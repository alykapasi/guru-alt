"""An author asks for their subject to be shared, and the snapshot freezes what would ship (S25b).

Two tests carry this file.

`test_the_snapshot_is_frozen_at_request_time` is what makes the review mean anything: the author
can go on editing between asking and being reviewed, so if the snapshot tracked the subject the
reviewer would approve one thing and ship another.

`test_the_snapshot_carries_no_private_material` asserts over the serialized blob rather than
field by field. A field-by-field test only checks the fields somebody thought of; this one fails
when a future change starts carrying something new, which is the failure actually worth catching.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Item, ItemKC, ItemOrigin, ItemType, Rubric
from app.models.content import ContentBlock, ContentType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.note import Note
from app.models.publication import Publication, PublicationStatus
from app.models.source import Source, SourceKind, SourceStatus
from app.services import publication as svc

API = "/api/v1"


async def _learner(session: AsyncSession, prefix: str) -> Learner:
    learner = Learner(handle=f"{prefix}-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _subject_with_graph(
    session: AsyncSession, owner: Learner, name: str = "Calculus"
) -> tuple[Subject, Topic, KC, KC]:
    tag = uuid.uuid4().hex[:8]
    subject = Subject(slug=f"s-{tag}", name=name, owner_learner_id=owner.id)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{tag}", name="Integrals")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{tag}", name="Substitution")
    prereq = KC(topic_id=topic.id, slug=f"p-{tag}", name="Antiderivatives")
    session.add_all([kc, prereq])
    await session.flush()
    session.add(KCEdge(prereq_kc_id=prereq.id, kc_id=kc.id))
    await session.flush()
    return subject, topic, kc, prereq


async def _item(session: AsyncSession, owner: Learner, kc_ids: list[uuid.UUID], stem: str) -> Item:
    item = Item(
        item_type=ItemType.MCQ,
        stem=stem,
        answer_key={"choices": ["a", "b"], "correct": 0},
        owner_learner_id=owner.id,
        author_learner_id=owner.id,
        origin=ItemOrigin.LEARNER,
        kc_links=[ItemKC(kc_id=kc_id) for kc_id in kc_ids],
    )
    session.add(item)
    await session.flush()
    return item


# --- who may ask -------------------------------------------------------------------------------


async def test_the_owner_can_request(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    await db_session.commit()

    response = await api_client.post(
        f"{API}/subjects/{subject.id}/publications", json={"note": "Please review."}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == PublicationStatus.PENDING
    assert body["author_note"] == "Please review."
    assert body["published_subject_id"] is None


async def test_a_stranger_gets_the_same_404_as_a_random_id(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """S25a's boundary, kept: a 403 here would confirm the subject exists."""
    stranger = await _learner(db_session, "b")
    theirs, _, _, _ = await _subject_with_graph(db_session, stranger)
    await db_session.commit()

    foreign = await api_client.post(f"{API}/subjects/{theirs.id}/publications", json={})
    missing = await api_client.post(f"{API}/subjects/{uuid.uuid4()}/publications", json={})

    assert foreign.status_code == 404
    assert foreign.json() == missing.json()


async def test_a_curated_subject_cannot_be_republished(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It is visible to everyone and writable by nobody. Publishing the shared library back
    into itself is not something the API should have an opinion about halfway through."""
    curated = Subject(slug=f"c-{uuid.uuid4().hex[:8]}", name="Shared")
    db_session.add(curated)
    await db_session.flush()
    await db_session.commit()

    response = await api_client.post(f"{API}/subjects/{curated.id}/publications", json={})

    assert response.status_code == 404


# --- what the snapshot contains -----------------------------------------------------------------


async def test_the_snapshot_is_frozen_at_request_time(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The reason a snapshot exists at all (D1, D3).

    The author keeps editing after asking. If the stored snapshot moved with the subject, the
    reviewer would be approving a graph and shipping a different one.
    """
    subject, topic, _, _ = await _subject_with_graph(db_session, api_learner)
    await db_session.commit()

    created = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})
    assert created.status_code == 201, created.text

    db_session.add(KC(topic_id=topic.id, slug=f"late-{uuid.uuid4().hex[:6]}", name="Added Later"))
    subject.name = "Renamed After Asking"
    await db_session.commit()

    stored = await db_session.get(Publication, uuid.UUID(created.json()["id"]))
    assert stored is not None
    assert stored.snapshot["subject"]["name"] == "Calculus"
    assert "Added Later" not in json.dumps(stored.snapshot)


async def test_the_snapshot_carries_the_graph_and_the_authors_own_items(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, kc, prereq = await _subject_with_graph(db_session, api_learner)
    mine = await _item(db_session, api_learner, [kc.id], "My question")
    await db_session.commit()

    snapshot = await svc.snapshot_of(db_session, subject, api_learner)

    assert {k["name"] for k in snapshot["kcs"]} == {"Substitution", "Antiderivatives"}
    assert snapshot["edges"] == [
        {"prereq_kc_id": str(prereq.id), "kc_id": str(kc.id), "weight": 1.0}
    ]
    assert [i["id"] for i in snapshot["items"]] == [str(mine.id)]
    # The reviewer sees answer keys — that is the point of reviewing what ships (D3).
    assert snapshot["items"][0]["answer_key"] == {"choices": ["a", "b"], "correct": 0}


async def test_another_learners_item_on_the_same_kc_does_not_travel(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """Shipping it would publish their work without anybody having asked them."""
    subject, _, kc, _ = await _subject_with_graph(db_session, api_learner)
    stranger = await _learner(db_session, "b")
    theirs = await _item(db_session, stranger, [kc.id], "Their question")
    mine = await _item(db_session, api_learner, [kc.id], "My question")
    await db_session.commit()

    snapshot = await svc.snapshot_of(db_session, subject, api_learner)

    assert [i["id"] for i in snapshot["items"]] == [str(mine.id)]
    assert str(theirs.id) not in json.dumps(snapshot)


async def test_an_item_reaching_another_subject_does_not_travel(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """It would carry that other subject's structure into the shared library with it."""
    subject, _, kc, _ = await _subject_with_graph(db_session, api_learner)
    _other, _, other_kc, _ = await _subject_with_graph(db_session, api_learner, name="Physics")
    straddling = await _item(db_session, api_learner, [kc.id, other_kc.id], "Straddles both")
    await db_session.commit()

    snapshot = await svc.snapshot_of(db_session, subject, api_learner)

    assert str(straddling.id) not in json.dumps(snapshot)
    assert str(other_kc.id) not in json.dumps(snapshot)


async def test_a_cross_subject_edge_does_not_travel(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, kc, _ = await _subject_with_graph(db_session, api_learner)
    _other, _, other_kc, _ = await _subject_with_graph(db_session, api_learner, name="Physics")
    db_session.add(KCEdge(prereq_kc_id=other_kc.id, kc_id=kc.id))
    await db_session.commit()

    snapshot = await svc.snapshot_of(db_session, subject, api_learner)

    assert str(other_kc.id) not in json.dumps(snapshot)
    assert len(snapshot["edges"]) == 1


async def test_the_snapshot_carries_no_private_material(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """Asserted over the whole serialized blob, on purpose.

    A field-by-field test only covers the fields somebody remembered. This one fails the day a
    change starts carrying something new, which is the failure worth catching — D2 lists these
    exclusions and none of them should ever reach a shared library.
    """
    subject, topic, kc, _ = await _subject_with_graph(db_session, api_learner)

    note = Note(learner_id=api_learner.id, topic_id=topic.id, learner_authored_md="private notes")
    source = Source(
        learner_id=api_learner.id,
        kind=SourceKind.FILE,
        origin="private.pdf",
        status=SourceStatus.DONE,
        subject_id=subject.id,
        meta={},
    )
    block = ContentBlock(
        learner_id=api_learner.id,
        kc_ids=[kc.id],
        block_type=ContentType.LESSON,
        body="a private explanation",
        citations=[],
        cache_key=f"ck-{uuid.uuid4().hex}",
        model="fake",
    )
    db_session.add_all([note, source, block])
    await db_session.flush()
    await db_session.commit()

    snapshot = json.dumps(await svc.snapshot_of(db_session, subject, api_learner))

    for private_id in (note.id, source.id, block.id):
        assert str(private_id) not in snapshot
    assert "private notes" not in snapshot
    assert "a private explanation" not in snapshot
    assert "private.pdf" not in snapshot


# --- refusals ------------------------------------------------------------------------------------


async def test_a_source_derived_subject_is_refused(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    subject.private_source_derived = True
    await db_session.commit()

    response = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})

    assert response.status_code == 422
    assert response.json()["detail"] == svc.SOURCE_DERIVED_REFUSAL


async def test_a_subject_with_no_components_is_refused(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    bare = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Empty", owner_learner_id=api_learner.id)
    db_session.add(bare)
    await db_session.flush()
    await db_session.commit()

    response = await api_client.post(f"{API}/subjects/{bare.id}/publications", json={})

    assert response.status_code == 422
    assert response.json()["detail"] == svc.NO_KCS_REFUSAL


async def test_a_second_pending_request_is_a_409(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    await db_session.commit()

    first = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})
    second = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})

    assert first.status_code == 201
    assert second.status_code == 409


async def test_the_partial_index_is_what_actually_holds(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The service's check is for a civil message; two concurrent requests would both read
    "none pending". This asserts the database refuses the second regardless."""
    from sqlalchemy.exc import IntegrityError

    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    for _ in range(2):
        db_session.add(
            Publication(
                source_subject_id=subject.id,
                author_id=api_learner.id,
                author_handle=api_learner.handle,
                status=PublicationStatus.PENDING,
                snapshot={},
            )
        )

    with pytest.raises(IntegrityError, match="uq_publications_one_pending"):
        await db_session.flush()
    await db_session.rollback()


# --- cancelling -----------------------------------------------------------------------------------


async def test_the_author_can_cancel_while_pending(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    await db_session.commit()
    created = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})
    publication_id = created.json()["id"]

    cancelled = await api_client.post(f"{API}/publications/{publication_id}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == PublicationStatus.CANCELLED


async def test_cancelling_twice_is_refused(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """And the subject is askable again, which is the point of cancelling."""
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    await db_session.commit()
    created = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})
    publication_id = created.json()["id"]
    await api_client.post(f"{API}/publications/{publication_id}/cancel")

    again = await api_client.post(f"{API}/publications/{publication_id}/cancel")
    reasked = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={})

    assert again.status_code == 422
    assert reasked.status_code == 201


async def test_another_learners_request_cannot_be_cancelled(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    stranger = await _learner(db_session, "b")
    theirs, _, _, _ = await _subject_with_graph(db_session, stranger)
    publication = Publication(
        source_subject_id=theirs.id,
        author_id=stranger.id,
        author_handle=stranger.handle,
        status=PublicationStatus.PENDING,
        snapshot={},
    )
    db_session.add(publication)
    await db_session.flush()
    await db_session.commit()

    foreign = await api_client.post(f"{API}/publications/{publication.id}/cancel")
    missing = await api_client.post(f"{API}/publications/{uuid.uuid4()}/cancel")

    assert foreign.status_code == 404
    assert foreign.json() == missing.json()


async def test_the_history_lists_every_request_for_the_subject(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    await db_session.commit()
    first = await api_client.post(f"{API}/subjects/{subject.id}/publications", json={"note": "one"})
    await api_client.post(f"{API}/publications/{first.json()['id']}/cancel")
    await api_client.post(f"{API}/subjects/{subject.id}/publications", json={"note": "two"})

    history = await api_client.get(f"{API}/subjects/{subject.id}/publications")

    assert history.status_code == 200
    entries = history.json()["publications"]
    assert {p["author_note"] for p in entries} == {"one", "two"}
    assert {p["status"] for p in entries} == {"cancelled", "pending"}


async def test_the_history_is_newest_first(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Ordering asserted on rows with distinct timestamps, set explicitly.

    Every request in an API test shares one transaction, and Postgres' ``now()`` is the
    *transaction's* clock — so rows written by three calls in one test all carry the same
    ``created_at`` and "newest" has nothing to order by. In production each request is its own
    transaction, which is the case this pins, without pretending the test harness reproduces it.
    """
    subject, _, _, _ = await _subject_with_graph(db_session, api_learner)
    older = Publication(
        source_subject_id=subject.id,
        author_id=api_learner.id,
        author_handle=api_learner.handle,
        status=PublicationStatus.REJECTED,
        author_note="older",
        snapshot={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    )
    newer = Publication(
        source_subject_id=subject.id,
        author_id=api_learner.id,
        author_handle=api_learner.handle,
        status=PublicationStatus.PENDING,
        author_note="newer",
        snapshot={},
        created_at=datetime(2026, 6, 1, tzinfo=UTC).replace(tzinfo=None),
    )
    db_session.add_all([older, newer])
    await db_session.flush()
    await db_session.commit()

    history = await api_client.get(f"{API}/subjects/{subject.id}/publications")

    assert [p["author_note"] for p in history.json()["publications"]] == ["newer", "older"]


async def test_a_rubric_the_author_owns_travels_with_its_item(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    subject, _, kc, _ = await _subject_with_graph(db_session, api_learner)
    rubric = Rubric(kc_id=kc.id, owner_learner_id=api_learner.id, name="Marking", criteria={"a": 1})
    db_session.add(rubric)
    await db_session.flush()
    item = Item(
        item_type=ItemType.SHORT,
        stem="Explain substitution",
        owner_learner_id=api_learner.id,
        author_learner_id=api_learner.id,
        origin=ItemOrigin.LEARNER,
        rubric_id=rubric.id,
        kc_links=[ItemKC(kc_id=kc.id)],
    )
    db_session.add(item)
    await db_session.flush()
    await db_session.commit()

    snapshot = await svc.snapshot_of(db_session, subject, api_learner)

    assert [r["id"] for r in snapshot["rubrics"]] == [str(rubric.id)]
    assert snapshot["items"][0]["rubric_id"] == str(rubric.id)
