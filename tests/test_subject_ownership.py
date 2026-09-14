"""Whose curriculum is it? (S25)

Subjects were global and unscoped. Listing returned everybody's; a duplicate name was rejected
across all learners, so the first person to study Calculus took the name from everyone after
them; and any authenticated learner could add topics, components and prerequisite edges to any
subject — including one somebody else was actively being taught from.

The boundary has two sides and they answer differently on purpose. A *curated* subject openly
exists and is read-only through this API, so trying to edit it is a 403. Another learner's
private subject answers 404 to everything, because 403 would confirm it exists.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.services import knowledge as svc
from tests.conftest import sign_in

API = "/api/v1"


async def _subject(
    session: AsyncSession, *, owner: uuid.UUID | None, name: str = "Calculus"
) -> tuple[Subject, Topic, KC]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name=name, owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="Topic")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Component")
    session.add(kc)
    await session.flush()
    return subject, topic, kc


async def _other_learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


# --- visibility ---------------------------------------------------------------------------


async def test_listing_shows_curated_subjects_and_the_callers_own_but_not_a_strangers(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    stranger = await _other_learner(db_session)
    curated, _, _ = await _subject(db_session, owner=None, name="Curated Calculus")
    mine, _, _ = await _subject(db_session, owner=api_learner.id, name="My Calculus")
    theirs, _, _ = await _subject(db_session, owner=stranger.id, name="Their Calculus")
    await db_session.commit()

    r = await api_client.get(f"{API}/subjects")

    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert str(curated.id) in ids
    assert str(mine.id) in ids
    assert str(theirs.id) not in ids


async def test_another_learners_subject_is_not_found_rather_than_forbidden(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """403 would confirm the id names a real subject, which is the one thing a stranger must
    not be able to establish about someone else's private curriculum."""
    stranger = await _other_learner(db_session)
    theirs, topic, kc = await _subject(db_session, owner=stranger.id)
    await db_session.commit()

    assert (await api_client.get(f"{API}/subjects/{theirs.id}")).status_code == 404
    assert (await api_client.get(f"{API}/subjects/{theirs.id}/topics")).status_code == 404
    assert (await api_client.get(f"{API}/topics/{topic.id}/kcs")).status_code == 404
    assert (await api_client.get(f"{API}/kcs/{kc.id}")).status_code == 404
    assert (await api_client.get(f"{API}/subjects/{theirs.id}/coverage")).status_code == 404


async def test_a_curated_subject_is_readable_by_anyone(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    curated, topic, kc = await _subject(db_session, owner=None)
    await db_session.commit()

    assert (await api_client.get(f"{API}/subjects/{curated.id}")).status_code == 200
    assert (await api_client.get(f"{API}/topics/{topic.id}/kcs")).status_code == 200
    assert (await api_client.get(f"{API}/kcs/{kc.id}")).status_code == 200


# --- writing ------------------------------------------------------------------------------


async def test_a_curated_subject_cannot_be_edited_through_the_learner_api(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The shared library stops being shared the moment any authenticated learner can add
    components to it."""
    curated, topic, kc = await _subject(db_session, owner=None)
    other = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Another")
    db_session.add(other)
    await db_session.commit()

    topic_r = await api_client.post(
        f"{API}/subjects/{curated.id}/topics", json={"slug": "new", "name": "New"}
    )
    kc_r = await api_client.post(
        f"{API}/topics/{topic.id}/kcs", json={"slug": "new", "name": "New"}
    )
    edge_r = await api_client.post(
        f"{API}/kcs/{kc.id}/prerequisites", json={"prereq_kc_id": str(other.id)}
    )
    del_r = await api_client.delete(f"{API}/kcs/{kc.id}/prerequisites/{other.id}")

    assert [topic_r.status_code, kc_r.status_code, edge_r.status_code, del_r.status_code] == [
        403,
        403,
        403,
        403,
    ]
    assert "read-only" in topic_r.json()["detail"]


async def test_another_learners_subject_cannot_be_edited_and_does_not_admit_it_exists(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    stranger = await _other_learner(db_session)
    theirs, topic, kc = await _subject(db_session, owner=stranger.id)
    other = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Another")
    db_session.add(other)
    await db_session.commit()

    topic_r = await api_client.post(
        f"{API}/subjects/{theirs.id}/topics", json={"slug": "new", "name": "New"}
    )
    kc_r = await api_client.post(
        f"{API}/topics/{topic.id}/kcs", json={"slug": "new", "name": "New"}
    )
    edge_r = await api_client.post(
        f"{API}/kcs/{kc.id}/prerequisites", json={"prereq_kc_id": str(other.id)}
    )

    assert [topic_r.status_code, kc_r.status_code, edge_r.status_code] == [404, 404, 404]


async def test_the_owner_can_edit_their_own_subject(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The boundary is worthless if it also stops the person it belongs to."""
    mine, topic, kc = await _subject(db_session, owner=api_learner.id)
    other = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Another")
    db_session.add(other)
    await db_session.commit()

    topic_r = await api_client.post(
        f"{API}/subjects/{mine.id}/topics", json={"slug": "new", "name": "New"}
    )
    kc_r = await api_client.post(
        f"{API}/topics/{topic.id}/kcs", json={"slug": "new-kc", "name": "New KC"}
    )
    edge_r = await api_client.post(
        f"{API}/kcs/{kc.id}/prerequisites", json={"prereq_kc_id": str(other.id)}
    )

    assert [topic_r.status_code, kc_r.status_code, edge_r.status_code] == [201, 201, 201]


async def test_an_edge_is_refused_when_only_the_far_end_belongs_to_someone_else(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Checking only the dependent would let a learner attach their own component to a
    stranger's as a prerequisite — an edge constrains the order *both* are taught in, so it
    changes a curriculum that is not theirs however the request is addressed."""
    stranger = await _other_learner(db_session)
    _mine, _topic, mine_kc = await _subject(db_session, owner=api_learner.id, name="Mine")
    _theirs, _t2, their_kc = await _subject(db_session, owner=stranger.id, name="Theirs")
    await db_session.commit()

    r = await api_client.post(
        f"{API}/kcs/{mine_kc.id}/prerequisites", json={"prereq_kc_id": str(their_kc.id)}
    )

    assert r.status_code == 404
    assert (await db_session.scalar(select(KCEdge).where(KCEdge.kc_id == mine_kc.id))) is None


# --- naming -------------------------------------------------------------------------------


async def test_two_learners_can_each_study_a_subject_of_the_same_name(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The global check meant the first learner to study Calculus took the name from every
    learner after them, reported as though they had made a mistake."""
    stranger = await _other_learner(db_session)
    await _subject(db_session, owner=stranger.id, name="Calculus")
    await db_session.flush()

    assert (
        await svc.subject_name_exists(db_session, "Calculus", owner_learner_id=stranger.id) is True
    )
    assert (
        await svc.subject_name_exists(db_session, "Calculus", owner_learner_id=api_learner.id)
        is False
    )


async def test_the_same_learner_is_still_stopped_from_creating_it_twice(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    await _subject(db_session, owner=api_learner.id, name="Linear Algebra")
    await db_session.flush()

    assert (
        await svc.subject_name_exists(
            db_session, "  linear algebra  ", owner_learner_id=api_learner.id
        )
        is True
    )


async def test_a_curated_name_does_not_block_a_learners_own(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The shared library's Calculus and the learner's Calculus are different things."""
    await _subject(db_session, owner=None, name="Calculus")
    await db_session.flush()

    assert (
        await svc.subject_name_exists(db_session, "Calculus", owner_learner_id=api_learner.id)
        is False
    )
    assert await svc.subject_name_exists(db_session, "Calculus", owner_learner_id=None) is True


# --- a generated curriculum belongs to the learner it was generated for --------------------


async def test_a_committed_curriculum_is_owned_by_the_learner_who_committed_it(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    result = await svc.create_subject_with_graph(
        db_session,
        "Fluid Dynamics",
        None,
        [{"name": "Basics", "kcs": [{"name": "Viscosity"}]}],
        None,
        api_learner.id,
    )

    assert result.subject.owner_learner_id == api_learner.id
    visible = await svc.list_subjects(db_session, learner_id=api_learner.id)
    assert result.subject.id in {s.id for s in visible}


async def test_a_second_learner_does_not_see_the_first_ones_generated_curriculum(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    stranger = await _other_learner(db_session)
    await db_session.commit()
    result = await svc.create_subject_with_graph(
        db_session,
        "Quantum Mechanics",
        None,
        [{"name": "Basics", "kcs": [{"name": "Superposition"}]}],
        None,
        stranger.id,
    )

    visible = await svc.list_subjects(db_session, learner_id=api_learner.id)

    assert result.subject.id not in {s.id for s in visible}
    assert (await api_client.get(f"{API}/subjects/{result.subject.id}")).status_code == 404


async def test_deleting_a_learner_takes_their_own_subject_and_leaves_the_curated_one(
    db_session: AsyncSession,
) -> None:
    """The retention statement, executed. A curated subject surviving is the half that matters:
    closing one account must not empty the shared library for everybody else."""
    owner = await _other_learner(db_session)
    mine, _, _ = await _subject(db_session, owner=owner.id, name="Theirs to lose")
    curated, _, _ = await _subject(db_session, owner=None, name="Shared")
    await db_session.flush()

    await db_session.delete(owner)
    await db_session.flush()

    # Queried rather than `session.get`, which would answer out of the identity map and report
    # a row the database has already cascaded away as still present.
    remaining = set(
        (
            await db_session.scalars(
                select(Subject.id).where(Subject.id.in_([mine.id, curated.id]))
            )
        ).all()
    )
    assert mine.id not in remaining
    assert curated.id in remaining


async def test_signing_in_as_the_owner_is_what_makes_the_difference(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Same subject, same request, two learners — the only variable is who is asking."""
    owner = await _other_learner(db_session)
    theirs, _, _ = await _subject(db_session, owner=owner.id, name="Owned")
    await db_session.commit()

    await sign_in(anon_client, db_session, owner)
    await db_session.commit()

    assert (await anon_client.get(f"{API}/subjects/{theirs.id}")).status_code == 200
