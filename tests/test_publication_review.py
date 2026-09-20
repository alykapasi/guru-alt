"""An administrator reviews a publication, and approval ships what they saw (S25b).

The first test is the one this file exists for. Approval materializes the *snapshot*, and the
difference between that and re-reading the subject is invisible to every test that does not
edit the subject in between — so this one edits it heavily, then asserts the copy matches what
the reviewer was shown rather than what the author has now.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import AssessmentVisibility, Item, ItemKC, ItemOrigin, ItemType, Rubric
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.publication import Publication, PublicationStatus
from app.services import publication as svc

API = "/api/v1"


async def _author_with_pending(
    session: AsyncSession, *, with_rubric: bool = False
) -> tuple[Learner, Subject, Publication, list[Item]]:
    """An author, their subject, two items on it, and a pending request over the lot."""
    tag = uuid.uuid4().hex[:8]
    author = Learner(handle=f"a-{tag}")
    session.add(author)
    await session.flush()

    subject = Subject(slug=f"s-{tag}", name="Calculus", owner_learner_id=author.id)
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

    rubric = None
    if with_rubric:
        rubric = Rubric(
            kc_id=kc.id, owner_learner_id=author.id, name="Marking", criteria={"clarity": 1}
        )
        session.add(rubric)
        await session.flush()

    items = []
    for label in ("kept", "struck"):
        item = Item(
            item_type=ItemType.MCQ,
            stem=f"The {label} question",
            answer_key={"choices": ["a", "b"], "correct": 0},
            owner_learner_id=author.id,
            author_learner_id=author.id,
            origin=ItemOrigin.LEARNER,
            rubric_id=rubric.id if (rubric and label == "kept") else None,
            kc_links=[ItemKC(kc_id=kc.id, weight=0.75)],
        )
        session.add(item)
        items.append(item)
    await session.flush()

    publication = await svc.request_publication(session, subject, author, "Please review.")
    return author, subject, publication, items


async def _copy_of(session: AsyncSession, publication_id: uuid.UUID) -> Subject:
    """Expires the identity map first, so this reads what the request actually committed.

    Callers must capture any id they still need *before* calling this: reading an attribute off
    an expired instance triggers a synchronous refresh, which fails outside the greenlet.
    """
    session.expire_all()
    stored = await session.get(Publication, publication_id)
    assert stored is not None and stored.published_subject_id is not None
    copy = await session.get(Subject, stored.published_subject_id)
    assert copy is not None
    return copy


async def _kcs_of(session: AsyncSession, subject: Subject) -> list[KC]:
    return list(
        await session.scalars(
            select(KC).join(Topic, KC.topic_id == Topic.id).where(Topic.subject_id == subject.id)
        )
    )


# --- the load-bearing one ------------------------------------------------------------------


async def test_approval_ships_the_snapshot_not_the_current_subject(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reviewer approved a graph; that graph is what must ship (D3).

    Approval that re-read the live subject would pass every test that leaves the subject alone
    between asking and approving — which is most of them. This one renames it, adds a
    component and deletes one, then checks the copy against what was frozen.
    """
    _author, subject, publication, _items = await _author_with_pending(db_session)
    topic = (await db_session.scalars(select(Topic).where(Topic.subject_id == subject.id))).one()
    subject.name = "Renamed After Asking"
    db_session.add(KC(topic_id=topic.id, slug=f"late-{uuid.uuid4().hex[:6]}", name="Added Later"))
    await db_session.commit()

    response = await admin_client.post(
        f"{API}/admin/publications/{publication.id}/approve", json={}
    )

    assert response.status_code == 200, response.text
    copy = await _copy_of(db_session, publication.id)
    assert copy.name == "Calculus", "the reviewer never saw the rename"
    assert {kc.name for kc in await _kcs_of(db_session, copy)} == {
        "Substitution",
        "Antiderivatives",
    }


# --- what the copy is ----------------------------------------------------------------------


async def test_the_copy_is_curated_and_owned_by_nobody(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()

    await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})

    copy = await _copy_of(db_session, publication.id)
    assert copy.owner_learner_id is None, "a NULL owner is what makes it shared (S25)"
    assert copy.publication_id == publication.id
    assert copy.private_source_derived is False


async def test_the_original_is_untouched_and_the_author_keeps_editing(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """D1: publishing is copying, not handing over."""
    _author, subject, publication, _items = await _author_with_pending(db_session)
    subject_id, publication_id = subject.id, publication.id
    await db_session.commit()

    await admin_client.post(f"{API}/admin/publications/{publication_id}/approve", json={})

    db_session.expire_all()
    original = await db_session.get(Subject, subject_id)
    assert original is not None
    assert original.owner_learner_id is not None
    assert original.publication_id is None


async def test_items_are_copied_as_curated_with_provenance_kept(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Ownership does not travel; authorship does. `origin` never grants authority."""
    author, _subject, publication, _items = await _author_with_pending(db_session)
    author_id, publication_id = author.id, publication.id
    await db_session.commit()

    await admin_client.post(f"{API}/admin/publications/{publication_id}/approve", json={})

    copy = await _copy_of(db_session, publication_id)
    kc_ids = [kc.id for kc in await _kcs_of(db_session, copy)]
    copied = list(
        await db_session.scalars(
            select(Item)
            .join(ItemKC, ItemKC.item_id == Item.id)
            .where(ItemKC.kc_id.in_(kc_ids))
            .distinct()
        )
    )
    assert len(copied) == 2
    for item in copied:
        assert item.visibility == AssessmentVisibility.CURATED
        assert item.owner_learner_id is None
        assert item.origin == ItemOrigin.LEARNER
        assert item.author_learner_id == author_id


async def test_kc_links_point_at_the_copies_not_the_originals(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A link left pointing at the author's KC would tie the shared copy to their private
    graph — and deleting their subject would take the shared items' tags with it."""
    _author, subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()
    original_kc_ids = {kc.id for kc in await _kcs_of(db_session, subject)}

    await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})

    copy = await _copy_of(db_session, publication.id)
    copied_kc_ids = {kc.id for kc in await _kcs_of(db_session, copy)}
    curated_links = list(
        await db_session.scalars(
            select(ItemKC)
            .join(Item, Item.id == ItemKC.item_id)
            .where(Item.owner_learner_id.is_(None), ItemKC.kc_id.in_(copied_kc_ids))
        )
    )
    assert curated_links, "the copies have no KC links at all"
    assert all(link.kc_id not in original_kc_ids for link in curated_links)
    # The apportioning weight is part of the item, not decoration.
    assert {link.weight for link in curated_links} == {0.75}


async def test_the_prerequisite_edge_is_rebuilt_between_the_copies(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()

    await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})

    copy = await _copy_of(db_session, publication.id)
    by_name = {kc.name: kc for kc in await _kcs_of(db_session, copy)}
    edge = (
        await db_session.scalars(select(KCEdge).where(KCEdge.kc_id == by_name["Substitution"].id))
    ).one()
    assert edge.prereq_kc_id == by_name["Antiderivatives"].id


async def test_a_rubric_is_copied_as_curated_and_relinked(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    _author, _subject, publication, _items = await _author_with_pending(
        db_session, with_rubric=True
    )
    await db_session.commit()

    await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})

    copy = await _copy_of(db_session, publication.id)
    kc_ids = [kc.id for kc in await _kcs_of(db_session, copy)]
    rubric = (await db_session.scalars(select(Rubric).where(Rubric.kc_id.in_(kc_ids)))).one()
    assert rubric.visibility == AssessmentVisibility.CURATED
    assert rubric.owner_learner_id is None
    linked = list(
        await db_session.scalars(
            select(Item).where(Item.rubric_id == rubric.id, Item.owner_learner_id.is_(None))
        )
    )
    assert len(linked) == 1 and linked[0].stem == "The kept question"


async def test_excluded_items_are_absent_and_recorded(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    _author, _subject, publication, items = await _author_with_pending(db_session)
    struck_id = next(item.id for item in items if item.stem == "The struck question")
    publication_id = publication.id
    await db_session.commit()

    response = await admin_client.post(
        f"{API}/admin/publications/{publication_id}/approve",
        json={"excluded_item_ids": [str(struck_id)]},
    )

    assert response.status_code == 200, response.text
    copy = await _copy_of(db_session, publication_id)
    kc_ids = [kc.id for kc in await _kcs_of(db_session, copy)]
    stems = {
        item.stem
        for item in await db_session.scalars(
            select(Item)
            .join(ItemKC, ItemKC.item_id == Item.id)
            .where(ItemKC.kc_id.in_(kc_ids))
            .distinct()
        )
    }
    assert stems == {"The kept question"}
    stored = await db_session.get(Publication, publication_id)
    assert stored is not None and stored.excluded_item_ids == [str(struck_id)]


async def test_the_published_slug_does_not_collide_with_a_curated_one(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    db_session.add(Subject(slug="calculus", name="Calculus"))
    await db_session.flush()
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()

    await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})

    copy = await _copy_of(db_session, publication.id)
    assert copy.slug == "calculus_2"


# --- the decision record ---------------------------------------------------------------------


async def test_approval_records_who_decided_and_when(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    publication_id = publication.id
    await db_session.commit()

    await admin_client.post(
        f"{API}/admin/publications/{publication_id}/approve", json={"note": "Looks sound."}
    )

    db_session.expire_all()
    stored = await db_session.get(Publication, publication_id)
    assert stored is not None
    assert stored.status == PublicationStatus.APPROVED
    assert stored.reviewer_handle is not None
    assert stored.reviewed_at is not None
    assert stored.review_note == "Looks sound."


async def test_rejection_requires_a_note_with_something_in_it(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """ "No" with nothing attached tells the author only that somebody looked."""
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()

    short = await admin_client.post(
        f"{API}/admin/publications/{publication.id}/reject", json={"note": "no"}
    )
    proper = await admin_client.post(
        f"{API}/admin/publications/{publication.id}/reject",
        json={"note": "The answer keys are wrong on three items."},
    )

    assert short.status_code == 422
    assert proper.status_code == 200
    assert proper.json()["status"] == PublicationStatus.REJECTED


async def test_the_author_sees_the_rejection_note(
    admin_client: AsyncClient,
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    """A refusal the author cannot read is a refusal they will repeat."""
    tag = uuid.uuid4().hex[:8]
    subject = Subject(slug=f"s-{tag}", name="Mine", owner_learner_id=api_learner.id)
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{tag}", name="T")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KC(topic_id=topic.id, slug=f"k-{tag}", name="K"))
    await db_session.flush()
    publication = await svc.request_publication(db_session, subject, api_learner, None)
    await db_session.commit()

    await admin_client.post(
        f"{API}/admin/publications/{publication.id}/reject",
        json={"note": "Needs worked examples."},
    )
    history = await api_client.get(f"{API}/subjects/{subject.id}/publications")

    assert history.status_code == 200
    entry = history.json()["publications"][0]
    assert entry["status"] == PublicationStatus.REJECTED
    assert entry["review_note"] == "Needs worked examples."


async def test_a_decided_request_cannot_be_decided_again(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()
    await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})

    again = await admin_client.post(f"{API}/admin/publications/{publication.id}/approve", json={})
    rejected = await admin_client.post(
        f"{API}/admin/publications/{publication.id}/reject",
        json={"note": "Changed my mind entirely."},
    )

    assert again.status_code == 422
    assert rejected.status_code == 422


async def test_the_queue_shows_the_snapshot_and_who_asked(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reviewer is not anonymised from the author (D6 is about other learners)."""
    author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()

    queue = await admin_client.get(f"{API}/admin/publications?status=pending")
    detail = await admin_client.get(f"{API}/admin/publications/{publication.id}")

    assert queue.status_code == 200
    assert str(publication.id) in {p["id"] for p in queue.json()["publications"]}
    body = detail.json()
    assert body["author_handle"] == author.handle
    assert body["author_note"] == "Please review."
    # Answer keys included: reviewing what ships means seeing it (D3).
    assert body["snapshot"]["items"][0]["answer_key"] is not None


# --- who may review ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("get", "", None),
        ("get", "/{id}", None),
        ("post", "/{id}/approve", {}),
        ("post", "/{id}/reject", {"note": "a long enough note"}),
    ],
)
async def test_an_ordinary_learner_is_refused_on_every_review_route(
    api_client: AsyncClient, db_session: AsyncSession, method: str, suffix: str, body: dict | None
) -> None:
    _author, _subject, publication, _items = await _author_with_pending(db_session)
    await db_session.commit()
    path = f"{API}/admin/publications{suffix.format(id=publication.id)}"

    response = await (api_client.get(path) if method == "get" else api_client.post(path, json=body))

    assert response.status_code == 403, f"{method.upper()} {path} answered {response.status_code}"
