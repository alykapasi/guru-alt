"""Assessment privacy regression at service and authenticated HTTP boundaries."""

import uuid

from app.models.assessment import Item, ItemKC, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.services import assessment as svc


async def test_unowned_generated_item_is_not_a_shared_bank(db_session, api_client, api_learner):
    subject = Subject(slug=f"privacy-{uuid.uuid4().hex}", name="Shared curriculum")
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="topic", name="Topic")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug="kc", name="KC")
    db_session.add(kc)
    await db_session.flush()
    item = Item(
        item_type=ItemType.MCQ,
        stem="Private generation",
        answer_key={"choices": ["a", "b"], "correct": 0},
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add(ItemKC(item_id=item.id, kc_id=kc.id))
    await db_session.commit()
    assert await svc.get_item_for(db_session, item.id, learner_id=api_learner.id) is None
    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=api_learner.id) is None
    assert (await api_client.get(f"/api/v1/items/{item.id}")).status_code == 404
    assert (
        await api_client.post(f"/api/v1/items/{item.id}/answer", json={"response": {"choice": 0}})
    ).status_code == 404


async def _curriculum(session, owner=None):
    subject = Subject(slug=f"privacy-{uuid.uuid4().hex}", name="Curriculum", owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="topic", name="Topic")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="kc", name="KC")
    session.add(kc)
    await session.flush()
    return subject, kc


async def test_owned_generation_and_private_rubric_cannot_be_reused_by_stranger(
    db_session, api_client, api_learner
):
    import pytest

    from app.learning.grading import InvalidResponse
    from app.learning.item_generation import generate_short_item
    from app.llm.registry import fake_llm_client
    from app.models.learner import Learner
    from app.schemas.assessment import AnswerSubmit

    other = Learner(handle=f"other-{uuid.uuid4().hex}")
    db_session.add(other)
    await db_session.flush()
    _, kc = await _curriculum(db_session)
    llm = fake_llm_client(reply='{"stem":"Explain X", "criteria":["Names X"]}')
    item, _ = await generate_short_item(db_session, llm, kc, owner_learner_id=other.id)
    assert item is not None
    assert item.rubric_id is not None
    assert item.owner_learner_id == other.id
    assert item.visibility == "private"
    rubric = await svc.get_rubric_for(db_session, item.rubric_id, learner_id=other.id)
    assert rubric is not None
    assert rubric.owner_learner_id == other.id
    assert await svc.get_rubric_for(db_session, rubric.id, learner_id=api_learner.id) is None
    assert await svc.get_item_for(db_session, item.id, learner_id=api_learner.id) is None
    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=api_learner.id) is None
    with pytest.raises(InvalidResponse, match="item not found"):
        await svc.answer_item(
            db_session, api_learner.id, item, AnswerSubmit(response={"text": "X"}), llm=llm
        )
    assert (await api_client.get(f"/api/v1/items/{item.id}")).status_code == 404
    assert (
        await api_client.post(f"/api/v1/items/{item.id}/answer", json={"response": {"text": "X"}})
    ).status_code == 404
    r = await api_client.post(
        "/api/v1/items",
        json={
            "item_type": "short",
            "stem": "Stolen rubric",
            "kcs": [{"kc_id": str(kc.id)}],
            "rubric_id": str(rubric.id),
        },
    )
    assert r.status_code == 404


async def test_explicit_curated_item_requires_authorized_subject_and_rubric(
    db_session, api_client, api_learner
):
    from app.models.assessment import AssessmentVisibility, Rubric
    from app.models.learner import Learner

    subject, kc = await _curriculum(db_session)
    rubric = Rubric(
        kc_id=kc.id, visibility=AssessmentVisibility.CURATED, criteria={"criteria": ["X"]}
    )
    db_session.add(rubric)
    await db_session.flush()
    item = Item(
        item_type="short",
        stem="Approved",
        rubric_id=rubric.id,
        visibility="curated",
        kc_links=[ItemKC(kc_id=kc.id)],
    )
    db_session.add(item)
    await db_session.commit()
    assert (
        await svc.get_item_for(
            db_session, item.id, learner_id=api_learner.id, subject_id=subject.id
        )
        is item
    )
    assert (
        await svc.get_item_for(
            db_session, item.id, learner_id=api_learner.id, subject_id=uuid.uuid4()
        )
        is None
    )
    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=api_learner.id) is item
    assert (await api_client.get(f"/api/v1/items/{item.id}")).status_code == 200
    other = Learner(handle=f"other-{uuid.uuid4().hex}")
    db_session.add(other)
    await db_session.flush()
    subject.owner_learner_id = other.id
    await db_session.commit()
    assert await svc.get_item_for(db_session, item.id, learner_id=api_learner.id) is None
    assert await svc.get_rubric_for(db_session, rubric.id, learner_id=api_learner.id) is None
    assert await svc.find_item_for_kc(db_session, kc.id, learner_id=api_learner.id) is None
    assert (await api_client.get(f"/api/v1/items/{item.id}")).status_code == 404
    assert (
        await api_client.post(f"/api/v1/items/{item.id}/answer", json={"response": {"text": "X"}})
    ).status_code == 404


async def test_legacy_backfill_only_infers_positive_private_ownership(
    db_session, api_learner, monkeypatch
):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import text

    from app.models.assessment import Rubric

    spec = importlib.util.spec_from_file_location(
        "assessment_migration", Path("db/migrations/versions/0052_assessment_ownership.py")
    )
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    _, private_kc = await _curriculum(db_session, api_learner.id)
    _, shared_kc = await _curriculum(db_session)
    rubric = Rubric(kc_id=private_kc.id, criteria={"criteria": ["private"]})
    db_session.add(rubric)
    await db_session.flush()
    private = Item(
        item_type="short",
        stem="Private legacy",
        rubric_id=rubric.id,
        kc_links=[ItemKC(kc_id=private_kc.id)],
    )
    unknown = Item(item_type="short", stem="Unknown legacy", kc_links=[ItemKC(kc_id=shared_kc.id)])
    authored = Item(
        item_type="short",
        stem="Authored legacy",
        author_learner_id=api_learner.id,
        kc_links=[ItemKC(kc_id=shared_kc.id)],
    )
    ambiguous = Item(
        item_type="short",
        stem="Mixed graph legacy",
        kc_links=[ItemKC(kc_id=shared_kc.id), ItemKC(kc_id=private_kc.id)],
    )
    db_session.add_all([private, unknown, authored, ambiguous])
    await db_session.flush()
    # Already-upgraded transaction: execute exactly the migration's real backfill SQL,
    # skipping schema DDL, to verify existing-data inference without destructive downgrade.
    statements = []

    class Operations:
        def add_column(self, *args, **kwargs):
            pass

        def create_foreign_key(self, *args, **kwargs):
            pass

        def create_index(self, *args, **kwargs):
            pass

        def execute(self, sql):
            statements.append(sql)

    monkeypatch.setattr(migration, "op", Operations())
    migration.upgrade()
    for sql in statements:
        await db_session.execute(text(sql))
    for row in [private, unknown, authored, ambiguous, rubric]:
        await db_session.refresh(row)
        assert row.visibility == "private"
    assert (
        private.owner_learner_id
        == rubric.owner_learner_id
        == authored.owner_learner_id
        == api_learner.id
    )
    assert unknown.owner_learner_id is None
    assert ambiguous.owner_learner_id is None


async def test_learner_cannot_publish_with_payload_flag_or_attach_foreign_curriculum(
    db_session, api_client, api_learner
):
    from app.models.learner import Learner

    other = Learner(handle=f"other-{uuid.uuid4().hex}")
    db_session.add(other)
    await db_session.flush()
    _, own_kc = await _curriculum(db_session, api_learner.id)
    _, foreign_kc = await _curriculum(db_session, other.id)
    body = {
        "item_type": "short",
        "stem": "Private",
        "kcs": [{"kc_id": str(own_kc.id)}],
        "visibility": "curated",
        "origin": "generated",
    }
    response = await api_client.post("/api/v1/items", json=body)
    assert response.status_code == 201
    row = await db_session.get(Item, uuid.UUID(response.json()["id"]))
    assert row.visibility == "private"
    assert row.owner_learner_id == api_learner.id
    body["kcs"] = [{"kc_id": str(foreign_kc.id)}]
    assert (await api_client.post("/api/v1/items", json=body)).status_code == 404


async def test_generating_foreign_private_kc_rejected_before_model_and_rubric_write(
    db_session, api_learner, monkeypatch
):
    import pytest
    from sqlalchemy import func, select

    from app.learning.grading import InvalidResponse
    from app.learning.item_generation import generate_short_item
    from app.llm.registry import fake_llm_client
    from app.models.assessment import Rubric
    from app.models.learner import Learner

    other = Learner(handle=f"other-{uuid.uuid4().hex}")
    db_session.add(other)
    await db_session.flush()
    _, kc = await _curriculum(db_session, other.id)
    client = fake_llm_client(reply='{"stem":"Secret", "criteria":["Secret criterion"]}')
    calls = []
    original = client.complete

    async def recording(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(client, "complete", recording)
    before = await db_session.scalar(select(func.count()).select_from(Rubric))
    with pytest.raises(InvalidResponse):
        await generate_short_item(db_session, client, kc, owner_learner_id=api_learner.id)
    assert calls == []
    assert await db_session.scalar(select(func.count()).select_from(Rubric)) == before


async def test_private_generated_assessment_export_and_erasure(db_session, api_learner):
    from app.learning.item_generation import generate_short_item
    from app.llm.registry import fake_llm_client
    from app.models.assessment import Rubric
    from app.services.retention import delete_learner, export_learner
    from app.storage.memory import InMemoryBlobStore

    _, kc = await _curriculum(db_session)
    item, _ = await generate_short_item(
        db_session,
        fake_llm_client(reply='{"stem":"X", "criteria":["Y"]}'),
        kc,
        owner_learner_id=api_learner.id,
    )
    assert item is not None
    rubric_id = item.rubric_id
    item_id = item.id
    exported = await export_learner(db_session, api_learner.id)
    assert any(str(row["id"]) == str(item_id) for row in exported["authored_items"])
    assert any(str(row["id"]) == str(rubric_id) for row in exported["rubrics"])
    await delete_learner(db_session, InMemoryBlobStore(), api_learner.id)
    assert await db_session.get(Item, item_id) is None
    assert await db_session.get(Rubric, rubric_id) is None


async def _flashcard_for(session, owner, back="Sugars and oxygen."):
    _, kc = await _curriculum(session, owner=owner)
    item = Item(
        item_type=ItemType.FLASHCARD,
        stem="What does photosynthesis produce?",
        answer_key={"back": back},
        owner_learner_id=owner,
    )
    session.add(item)
    await session.flush()
    session.add(ItemKC(item_id=item.id, kc_id=kc.id))
    await session.commit()
    return item


async def test_a_flashcard_back_is_not_shipped_with_the_question(
    db_session, api_client, api_learner
):
    """Withheld before reveal, so the reveal is a real step and not an animation.

    The key name *and* the answer text. Pinning only the name lets a presentation that
    surfaced the same string under some other key leak it with this test still green — and
    the leak, not the spelling of the field, is the thing being prevented.
    """
    secret = "Glucose-and-molecular-oxygen-zzq"
    item = await _flashcard_for(db_session, api_learner.id, back=secret)
    r = await api_client.get(f"/api/v1/items/{item.id}")
    assert r.status_code == 200, r.text
    body = str(r.json())
    assert "back" not in body
    assert secret not in body


async def test_reveal_returns_the_back(db_session, api_client, api_learner):
    item = await _flashcard_for(db_session, api_learner.id)
    r = await api_client.post(f"/api/v1/items/{item.id}/reveal")
    assert r.status_code == 200, r.text
    assert r.json()["back"] == "Sugars and oxygen."


async def test_reveal_refuses_a_non_flashcard(db_session, api_client, api_learner):
    """There is nothing to reveal on an MCQ but its answer key, and that is the one thing a
    reveal endpoint must never be able to hand out."""
    _, kc = await _curriculum(db_session, owner=api_learner.id)
    item = Item(
        item_type=ItemType.MCQ,
        stem="Pick one",
        answer_key={"choices": ["a", "b"], "correct": 0},
        owner_learner_id=api_learner.id,
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add(ItemKC(item_id=item.id, kc_id=kc.id))
    await db_session.commit()

    r = await api_client.post(f"/api/v1/items/{item.id}/reveal")
    assert r.status_code == 422, r.text


async def test_reveal_refuses_another_learners_item(db_session, api_client, api_learner):
    """Same scoping as every other item read — `get_item_for`, not a bare primary-key load."""
    from app.models.learner import Learner

    stranger = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    item = await _flashcard_for(db_session, stranger.id)

    r = await api_client.post(f"/api/v1/items/{item.id}/reveal")
    assert r.status_code == 404, r.text


async def test_paused_question_from_other_subject_is_not_current(db_session, api_learner):
    from app.services.checkpoints import paused_practice_is_current

    subject, kc = await _curriculum(db_session, api_learner.id)
    item = Item(
        item_type="short",
        stem="Private question",
        owner_learner_id=api_learner.id,
        kc_links=[ItemKC(kc_id=kc.id)],
    )
    db_session.add(item)
    await db_session.commit()
    assert not await paused_practice_is_current(
        db_session, learner_id=api_learner.id, item_id=str(item.id), subject_id=uuid.uuid4()
    )
    assert await paused_practice_is_current(
        db_session, learner_id=api_learner.id, item_id=str(item.id), subject_id=subject.id
    )
