"""A subject built from private uploads can never be published (S25b D4).

The test that carries this file is `test_a_client_cannot_opt_out_by_omission`. The flag used to
be a bit the browser carried from curriculum generation back to commit, and a bit you hand the
client is a bit the client can drop — which makes the control an override with extra steps
rather than a control. It is read from a server-side row now, and this asserts that a request
saying nothing at all about grounding still gets flagged.

The opposite failure has its own test too. Flagging a subject that no source text ever reached
would retire an honest curriculum for no privacy gain, and a flag that creeps ends up on
everything.
"""

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm.registry import fake_llm_client
from app.llm.types import ModelRole
from app.main import app
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.publication import CurriculumProposal
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from tests.embedding import FAKE_SPACE

API = "/api/v1"

CURRICULUM_REPLY = """{
    "subject_name": "Linear Algebra",
    "subject_description": "Vectors and the maps between them",
    "topics": [
        {
            "name": "Vectors",
            "description": "Vector spaces",
            "kcs": [{"name": "Vector Addition", "description": "Adding vectors"}]
        }
    ]
}"""

GOAL = "Learn linear algebra"


@pytest.fixture
def fake_llm_curriculum() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(CURRICULUM_REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def _retrievable_source(session: AsyncSession, learner: Learner) -> Source:
    """A source with one embedded chunk the goal's keywords will actually retrieve.

    Retrieval has to return something, or the generation is not grounded and this fixture
    would be testing the wrong branch — see `test_source_ids_that_retrieve_nothing_are_not_...`.
    """
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="linear_algebra.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        meta={},
    )
    session.add(source)
    await session.flush()

    text = "linear algebra fundamentals: vectors and vector spaces, matrices and transformations"
    llm = fake_llm_client(CURRICULUM_REPLY)
    embedding = (await llm.embed(ModelRole.EMBED, [text])).vectors[0]
    session.add(
        Chunk(
            embedding_space=FAKE_SPACE,
            source_id=source.id,
            ordinal=0,
            text=text,
            embedding=embedding,
            provenance={"source_id": str(source.id), "method": "text"},
        )
    )
    await session.flush()
    return source


async def _generate(client: AsyncClient, *, source_ids: list[str] | None = None) -> str:
    """Generate a curriculum and return the server-issued proposal id."""
    response = await client.post(
        f"{API}/onboarding/curriculum", json={"goal": GOAL, "source_ids": source_ids}
    )
    assert response.status_code == 200, response.text
    return response.json()["proposal_id"]


async def _commit(
    client: AsyncClient, proposal_id: str | None, *, source_ids: list[str] | None = None
) -> Response:
    body: dict = {
        "subject_name": "Linear Algebra",
        "subject_description": None,
        "topics": [{"name": "Vectors", "description": None, "kcs": []}],
        "source_ids": source_ids,
    }
    if proposal_id is not None:
        body["proposal_id"] = proposal_id
    return await client.post(f"{API}/subjects/commit", json=body)


async def _flag_of(session: AsyncSession, subject_id: str) -> bool:
    session.expire_all()
    subject = await session.get(Subject, uuid.UUID(subject_id))
    assert subject is not None
    return subject.private_source_derived


# --- generation, recorded server-side --------------------------------------------------------


async def test_a_generation_that_used_excerpts_flags_the_subject(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    fake_llm_curriculum: None,
) -> None:
    source = await _retrievable_source(db_session, api_learner)

    proposal_id = await _generate(api_client, source_ids=[str(source.id)])
    committed = await _commit(api_client, proposal_id)

    assert committed.status_code == 201, committed.text
    assert await _flag_of(db_session, committed.json()["id"]) is True


async def test_a_generation_with_no_sources_does_not_flag(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    fake_llm_curriculum: None,
) -> None:
    """A flag that creeps ends up on everything, and then nothing is publishable."""
    proposal_id = await _generate(api_client)
    committed = await _commit(api_client, proposal_id)

    assert committed.status_code == 201, committed.text
    assert await _flag_of(db_session, committed.json()["id"]) is False


async def test_source_ids_that_retrieve_nothing_are_not_grounding(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    fake_llm_curriculum: None,
) -> None:
    """Asking for sources is not the same as the model reading them.

    A source with no chunks retrieves nothing, so no source text reached the curriculum. The
    subject is not source-derived, and saying it was would retire an honest one.
    """
    empty = Source(
        learner_id=api_learner.id,
        kind=SourceKind.FILE,
        origin="empty.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        meta={},
    )
    db_session.add(empty)
    await db_session.flush()

    proposal_id = await _generate(api_client, source_ids=[str(empty.id)])
    record = await db_session.get(CurriculumProposal, uuid.UUID(proposal_id))

    assert record is not None
    assert record.grounded_in_sources is False


# --- the omission attack ---------------------------------------------------------------------


async def test_a_client_cannot_opt_out_by_omission(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    fake_llm_curriculum: None,
) -> None:
    """The reason this whole mechanism moved server-side.

    The commit says nothing about grounding and passes no `source_ids` — exactly the request a
    client would send to launder a source-derived curriculum into a publishable subject. The
    proposal row remembers what the generation actually did, so the subject is flagged anyway.
    """
    source = await _retrievable_source(db_session, api_learner)
    proposal_id = await _generate(api_client, source_ids=[str(source.id)])

    committed = await _commit(api_client, proposal_id, source_ids=None)

    assert committed.status_code == 201, committed.text
    assert await _flag_of(db_session, committed.json()["id"]) is True


async def test_a_commit_without_a_proposal_id_is_refused(
    api_client: AsyncClient, fake_llm_curriculum: None
) -> None:
    """Required, not optional. An optional id is one the client can simply leave out, which is
    the same hole in a different shape."""
    assert (await _commit(api_client, None)).status_code == 422


async def test_another_learners_proposal_id_is_a_404(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm_curriculum: None
) -> None:
    """The same answer as an id that names nothing. A caller who could tell them apart could
    probe for other learners' activity, which is the shape of leak this slice closes."""
    stranger = Learner(handle=f"s-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    theirs = CurriculumProposal(learner_id=stranger.id, grounded_in_sources=True)
    db_session.add(theirs)
    await db_session.flush()

    borrowed = await _commit(api_client, str(theirs.id))
    unknown = await _commit(api_client, str(uuid.uuid4()))

    assert borrowed.status_code == 404
    assert unknown.status_code == 404
    assert borrowed.json() == unknown.json()


# --- the other triggers -----------------------------------------------------------------------


async def test_source_ids_at_commit_flag_the_subject(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    fake_llm_curriculum: None,
) -> None:
    """Independent of the proposal row: moving sources into a subject is itself grounding."""
    source = await _retrievable_source(db_session, api_learner)
    proposal_id = await _generate(api_client)  # not grounded
    await db_session.commit()

    committed = await _commit(api_client, proposal_id, source_ids=[str(source.id)])

    assert committed.status_code == 201, committed.text
    assert await _flag_of(db_session, committed.json()["id"]) is True


async def test_uploading_a_source_into_a_subject_flags_it(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The upload path, which reaches a subject without any curriculum generation at all."""
    subject = Subject(
        slug=f"s-{uuid.uuid4().hex[:8]}", name="Hand Built", owner_learner_id=api_learner.id
    )
    db_session.add(subject)
    await db_session.flush()
    subject_id = subject.id
    await db_session.commit()

    response = await api_client.post(
        f"{API}/sources/upload",
        files={"file": ("notes.txt", b"some private material", "text/plain")},
        data={"subject_id": str(subject_id)},
    )

    assert response.status_code in (200, 202), response.text
    assert await _flag_of(db_session, str(subject_id)) is True


async def test_the_flag_is_a_latch(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Running a trigger against an already-flagged subject is a no-op, not an error."""
    subject = Subject(
        slug=f"s-{uuid.uuid4().hex[:8]}",
        name="Already Flagged",
        owner_learner_id=api_learner.id,
        private_source_derived=True,
    )
    db_session.add(subject)
    await db_session.flush()
    subject_id = subject.id
    await db_session.commit()

    response = await api_client.post(
        f"{API}/sources/upload",
        files={"file": ("more.txt", b"more private material", "text/plain")},
        data={"subject_id": str(subject_id)},
    )

    assert response.status_code in (200, 202), response.text
    assert await _flag_of(db_session, str(subject_id)) is True


async def test_nothing_flags_a_subject_that_no_source_touched(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The baseline the other tests are measured against."""
    subject = Subject(
        slug=f"s-{uuid.uuid4().hex[:8]}", name="Untouched", owner_learner_id=api_learner.id
    )
    db_session.add(subject)
    await db_session.flush()

    assert subject.private_source_derived is False
    assert (
        await db_session.scalar(
            select(Subject.private_source_derived).where(Subject.id == subject.id)
        )
        is False
    )
