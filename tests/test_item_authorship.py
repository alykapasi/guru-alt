"""A learner-authored question cannot become another learner's assessment (S33).

``items`` is a global table and ``POST /items`` was open to any authenticated learner, so
writing a question — and its answer key — added it to the bank that bank-reuse draws other
learners' practice from. Being signed in is not authority to author an assessment other
people are graded against, and a mastery observation traced to a question nobody vouched for
is a measurement of nothing.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import Item, ItemKC, ItemOrigin, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.services import assessment as svc

API = "/api/v1"


async def _kc(session: AsyncSession) -> KC:
    slug = f"s-{uuid.uuid4().hex[:8]}"
    subject = Subject(name=slug, slug=slug)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, name="K", description="d", slug=f"k-{uuid.uuid4().hex[:8]}")
    session.add(kc)
    await session.flush()
    return kc


async def _other_learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"other-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _item(
    session: AsyncSession, kc: KC, *, origin: str, author_learner_id: uuid.UUID | None = None
) -> Item:
    item = Item(
        item_type=ItemType.MCQ,
        stem="Whose question is this?",
        answer_key={"choices": ["a", "b"], "correct": 0},
        origin=origin,
        author_learner_id=author_learner_id,
    )
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id))
    await session.commit()
    return item


# --- authoring records who wrote it --------------------------------------------------------


async def test_an_item_a_learner_writes_is_theirs_not_the_banks(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    kc = await _kc(db_session)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/items",
        json={
            "item_type": "mcq",
            "stem": "What is 2 + 2?",
            "answer_key": {"choices": ["3", "4"], "correct": 1},
            "kcs": [{"kc_id": str(kc.id), "weight": 1.0}],
        },
    )

    assert r.status_code == 201, r.text
    assert r.json()["origin"] == ItemOrigin.LEARNER
    item = await db_session.get(Item, uuid.UUID(r.json()["id"]))
    assert item is not None
    learner = api_learner
    assert item.author_learner_id == learner.id


async def test_the_platforms_own_generators_still_write_to_the_shared_bank(
    db_session: AsyncSession,
) -> None:
    """The restriction is about *authority*, not about locking the bank: generator output is
    what reuse is for, and it stays shared."""
    kc = await _kc(db_session)
    item = await _item(db_session, kc, origin=ItemOrigin.GENERATED)
    stranger = await _other_learner(db_session)

    found = await svc.find_item_for_kc(db_session, kc.id, learner_id=stranger.id)

    assert found is not None and found.id == item.id


# --- and nobody else is examined with it ---------------------------------------------------


async def test_bank_reuse_does_not_serve_someone_elses_question(
    db_session: AsyncSession,
) -> None:
    kc = await _kc(db_session)
    author = await _other_learner(db_session)
    await _item(db_session, kc, origin=ItemOrigin.LEARNER, author_learner_id=author.id)
    stranger = await _other_learner(db_session)

    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=stranger.id) is None
    # Its author still gets it back — this is a scope, not a quarantine.
    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=author.id) is not None


async def test_someone_elses_question_is_not_readable(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Reading it exposes the stem and, for an MCQ, the choices (S54)."""
    kc = await _kc(db_session)
    author = await _other_learner(db_session)
    item = await _item(db_session, kc, origin=ItemOrigin.LEARNER, author_learner_id=author.id)

    r = await api_client.get(f"{API}/items/{item.id}")

    assert r.status_code == 404


async def test_someone_elses_question_is_not_answerable(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Answering it would trace a mastery observation from a question nobody vouched for."""
    kc = await _kc(db_session)
    author = await _other_learner(db_session)
    item = await _item(db_session, kc, origin=ItemOrigin.LEARNER, author_learner_id=author.id)

    r = await api_client.post(f"{API}/items/{item.id}/answer", json={"response": {"choice": 0}})

    assert r.status_code == 404


async def test_deleting_an_author_does_not_release_their_items_to_everyone(
    db_session: AsyncSession,
) -> None:
    """``author_learner_id`` is ON DELETE SET NULL, so provenance cannot be carried by that
    column alone — a deleted author would otherwise publish every item they wrote."""
    kc = await _kc(db_session)
    author = await _other_learner(db_session)
    await db_session.commit()
    await _item(db_session, kc, origin=ItemOrigin.LEARNER, author_learner_id=author.id)

    await db_session.delete(author)
    await db_session.commit()

    stranger = await _other_learner(db_session)
    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=stranger.id) is None
