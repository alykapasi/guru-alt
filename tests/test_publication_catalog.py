"""Versions supersede, withdrawal unlists, and neither takes anything away (S25b D7).

The rule the catalog encodes is easy to state and easy to get half right: an old version
disappears from the *listing* while staying reachable, and it stays in the listing of anyone
already studying it. Half of that is the privacy-neutral half — it was reviewed as shareable,
so reaching it by id was never a leak — and half is the part learners would notice, because a
subject vanishing out from under a lesson plan is the failure nobody reports as a bug, they
just stop using it.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.lesson_plan import LessonPlan
from app.models.publication import Publication
from app.services import knowledge as knowledge_svc
from app.services import publication as svc

API = "/api/v1"


async def _author_subject(
    session: AsyncSession, name: str = "Calculus"
) -> tuple[uuid.UUID, uuid.UUID]:
    """An author and a publishable subject, returned as ids.

    Ids, not instances: publishing expires the identity map, so anything held across a publish
    is stale and reading it raises outside the greenlet. See `_publish`.
    """
    tag = uuid.uuid4().hex[:8]
    author = Learner(handle=f"a-{tag}")
    session.add(author)
    await session.flush()
    subject = Subject(slug=f"s-{tag}", name=name, owner_learner_id=author.id)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{tag}", name="Topic")
    session.add(topic)
    await session.flush()
    session.add(KC(topic_id=topic.id, slug=f"k-{tag}", name="Component"))
    await session.flush()
    return author.id, subject.id


async def _publish(
    session: AsyncSession,
    admin_client: AsyncClient,
    author_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> uuid.UUID:
    """Ask and approve, returning the published copy's id.

    Takes and returns ids rather than ORM instances on purpose. Approving expires the identity
    map, so an instance held across two calls is stale, and reading an attribute off it
    triggers a synchronous refresh that fails outside the greenlet. Ids do not go stale.
    """
    author = await session.get(Learner, author_id)
    subject = await session.get(Subject, subject_id)
    assert author is not None and subject is not None
    publication = await svc.request_publication(session, subject, author, None)
    publication_id = publication.id
    await session.commit()
    response = await admin_client.post(
        f"{API}/admin/publications/{publication_id}/approve", json={}
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    stored = await session.get(Publication, publication_id)
    assert stored is not None and stored.published_subject_id is not None
    return stored.published_subject_id


async def _catalog(session: AsyncSession, learner_id: uuid.UUID) -> set[uuid.UUID]:
    """Takes an id, not a `Learner`.

    `_publish` expires the identity map, and reading an attribute off an expired instance
    triggers a synchronous refresh that fails outside the greenlet. Every caller here captures
    the ids it needs before publishing.
    """
    return {s.id for s in await knowledge_svc.list_subjects(session, learner_id=learner_id)}


async def test_a_published_subject_is_in_every_learners_catalog(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id
    author_id, subject_id = await _author_subject(db_session)
    copy_id = await _publish(db_session, admin_client, author_id, subject_id)

    assert copy_id in await _catalog(db_session, learner_id)


async def test_republishing_supersedes_and_unlists_the_previous_version(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id
    author_id, subject_id = await _author_subject(db_session)
    first_id = await _publish(db_session, admin_client, author_id, subject_id)
    second_id = await _publish(db_session, admin_client, author_id, subject_id)

    db_session.expire_all()
    older = await db_session.get(Subject, first_id)
    assert older is not None and older.superseded_by_id == second_id
    catalog = await _catalog(db_session, learner_id)
    assert second_id in catalog
    assert first_id not in catalog


async def test_a_learner_with_a_plan_on_the_old_version_still_sees_it(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id
    """The half that would otherwise strand somebody mid-subject."""
    author_id, subject_id = await _author_subject(db_session)
    first_id = await _publish(db_session, admin_client, author_id, subject_id)
    db_session.add(LessonPlan(learner_id=learner_id, subject_id=first_id))
    await db_session.flush()

    await _publish(db_session, admin_client, author_id, subject_id)

    assert first_id in await _catalog(db_session, learner_id)


async def test_a_learner_without_a_plan_does_not_see_the_old_version(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id
    """The clause is per learner, not a global "somebody is studying it" escape hatch."""
    author_id, subject_id = await _author_subject(db_session)
    first_id = await _publish(db_session, admin_client, author_id, subject_id)
    studying = Learner(handle=f"c-{uuid.uuid4().hex[:8]}")
    db_session.add(studying)
    await db_session.flush()
    studying_id = studying.id
    db_session.add(LessonPlan(learner_id=studying_id, subject_id=first_id))
    await db_session.flush()

    await _publish(db_session, admin_client, author_id, subject_id)

    assert first_id in await _catalog(db_session, studying_id)
    assert first_id not in await _catalog(db_session, learner_id)


async def test_an_unlisted_version_is_still_reachable_by_id(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Deliberate (D7). It was reviewed as shareable, so reaching it is not a leak — and the
    alternative breaks every link and plan pointing at it."""
    author_id, subject_id = await _author_subject(db_session)
    first_id = await _publish(db_session, admin_client, author_id, subject_id)
    await _publish(db_session, admin_client, author_id, subject_id)
    await db_session.commit()

    response = await admin_client.get(f"{API}/subjects/{first_id}")

    assert response.status_code == 200


async def test_publishing_a_different_subject_supersedes_nothing(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Sharing your Physics is not a new version of your Calculus."""
    learner_id = api_learner.id
    author_id, calculus_source_id = await _author_subject(db_session, name="Calculus")
    calculus_id = await _publish(db_session, admin_client, author_id, calculus_source_id)

    tag = uuid.uuid4().hex[:8]
    physics = Subject(slug=f"s-{tag}", name="Physics", owner_learner_id=author_id)
    db_session.add(physics)
    await db_session.flush()
    topic = Topic(subject_id=physics.id, slug=f"t-{tag}", name="Topic")
    db_session.add(topic)
    await db_session.flush()
    db_session.add(KC(topic_id=topic.id, slug=f"k-{tag}", name="Component"))
    await db_session.flush()
    await _publish(db_session, admin_client, author_id, physics.id)

    db_session.expire_all()
    still_current = await db_session.get(Subject, calculus_id)
    assert still_current is not None and still_current.superseded_by_id is None
    assert calculus_id in await _catalog(db_session, learner_id)


# --- withdrawal ------------------------------------------------------------------------------


async def test_withdrawal_unlists_with_the_same_rule(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id
    author_id, subject_id = await _author_subject(db_session)
    copy_id = await _publish(db_session, admin_client, author_id, subject_id)
    await db_session.commit()

    response = await admin_client.post(
        f"{API}/admin/subjects/{copy_id}/withdraw", json={"reason": "Answer keys were wrong."}
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    withdrawn = await db_session.get(Subject, copy_id)
    assert withdrawn is not None
    assert withdrawn.withdrawn_at is not None
    assert withdrawn.withdrawn_reason == "Answer keys were wrong."
    assert copy_id not in await _catalog(db_session, learner_id)


async def test_a_learner_studying_a_withdrawn_subject_keeps_it(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id
    author_id, subject_id = await _author_subject(db_session)
    copy_id = await _publish(db_session, admin_client, author_id, subject_id)
    db_session.add(LessonPlan(learner_id=learner_id, subject_id=copy_id))
    await db_session.flush()
    await db_session.commit()

    await admin_client.post(
        f"{API}/admin/subjects/{copy_id}/withdraw", json={"reason": "Superseded by better."}
    )

    assert copy_id in await _catalog(db_session, learner_id)


async def test_withdrawal_needs_a_reason(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    author_id, subject_id = await _author_subject(db_session)
    copy_id = await _publish(db_session, admin_client, author_id, subject_id)
    await db_session.commit()

    blank = await admin_client.post(
        f"{API}/admin/subjects/{copy_id}/withdraw", json={"reason": "   "}
    )
    missing = await admin_client.post(f"{API}/admin/subjects/{copy_id}/withdraw", json={})

    assert blank.status_code == 422
    assert missing.status_code == 422


async def test_only_a_published_subject_can_be_withdrawn(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A learner's own subject and a seeded curated one are both refused.

    Withdrawing the first would be a deletion wearing another name; the second is an operator's
    migration, not a review action.
    """
    _author_id, owned_id = await _author_subject(db_session)
    seeded = Subject(slug=f"c-{uuid.uuid4().hex[:8]}", name="Seeded")
    db_session.add(seeded)
    await db_session.flush()
    seeded_id = seeded.id
    await db_session.commit()

    for subject_id in (owned_id, seeded_id):
        response = await admin_client.post(
            f"{API}/admin/subjects/{subject_id}/withdraw", json={"reason": "A stated reason."}
        )
        assert response.status_code == 422, subject_id
        assert response.json()["detail"] == svc.NOT_PUBLISHED_REFUSAL


async def test_a_non_admin_cannot_withdraw(
    api_client: AsyncClient, admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    author_id, subject_id = await _author_subject(db_session)
    copy_id = await _publish(db_session, admin_client, author_id, subject_id)
    await db_session.commit()

    response = await api_client.post(
        f"{API}/admin/subjects/{copy_id}/withdraw", json={"reason": "Not mine to do."}
    )

    assert response.status_code == 403


async def test_the_authors_own_subject_never_leaves_their_catalog(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Publishing copies; it does not move anything (D1)."""
    author_id, subject_id = await _author_subject(db_session)
    copy_id = await _publish(db_session, admin_client, author_id, subject_id)

    catalog = await _catalog(db_session, author_id)
    assert subject_id in catalog
    assert copy_id in catalog
