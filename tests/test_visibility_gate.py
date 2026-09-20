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
