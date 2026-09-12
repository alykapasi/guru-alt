"""Races that need two real connections and two real commits (S58).

Every other API test in this suite shares one session inside one transaction that is rolled
back at the end. That is the right default — it is fast and it leaves nothing behind — but it
cannot exercise the failures these tests are about, because two "concurrent" requests on one
savepoint-joined session are not concurrent and never commit. A unique constraint cannot be
violated, a lease cannot be lost to somebody else, and an idempotency key is checked against
writes the other request has not made yet.

So these use the app with its **own** session factory: each request opens its own connection
and commits for real, and cleanup is by deleting the learner and letting the cascade run. They
are slower and they are meant to be few — one per boundary where losing the race corrupts
something, rather than a second copy of the suite.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_engine, get_llm_client
from app.core import db as core_db
from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.source import Source, SourceKind, SourceStatus
from app.services import auth, ingestion
from tests.conftest import sign_in

API = "/api/v1"

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def live_learner(engine: AsyncEngine) -> AsyncIterator[Learner]:
    """A learner that really exists, committed on its own connection.

    Torn down by deleting the row: every learner-owned table cascades from it (S61), so one
    delete is the whole cleanup. What does *not* cascade is shared curriculum, which each test
    removes itself.
    """
    async with AsyncSession(engine, expire_on_commit=False) as session:
        learner = Learner(handle=f"cx-{uuid.uuid4().hex[:8]}", display_name="Cross Connection")
        session.add(learner)
        await session.commit()
    try:
        yield learner
    finally:
        async with AsyncSession(engine) as session:
            await session.execute(delete(Learner).where(Learner.id == learner.id))
            await session.commit()


@pytest_asyncio.fixture
async def live_client(engine: AsyncEngine, live_learner: Learner) -> AsyncIterator[AsyncClient]:
    """The app with `get_session` **not** overridden, so every request commits for real.

    The process-wide engine's pooled connections bind to whichever event loop first used them,
    and pytest-asyncio gives each test its own — so it is disposed on the way out rather than
    left to be reused from a loop that has closed.
    """
    app.dependency_overrides[get_engine] = lambda: engine
    # Called, not passed: handing FastAPI the factory itself makes it read `script` as a
    # request parameter, and every request body then fails validation against it.
    client_stub = fake_llm_client()
    app.dependency_overrides[get_llm_client] = lambda: client_stub
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with AsyncSession(engine) as session:
                await sign_in(client, session, live_learner)
                await session.commit()
            yield client
    finally:
        app.dependency_overrides.clear()
        await core_db.engine.dispose()


async def _seed_kc(engine: AsyncEngine) -> tuple[Subject, KC]:
    """Shared curriculum, committed. Returned so the test can delete it again."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Concurrency")
        session.add(subject)
        await session.flush()
        topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="Races")
        session.add(topic)
        await session.flush()
        kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Lost updates")
        session.add(kc)
        await session.commit()
    return subject, kc


async def _drop_subject(engine: AsyncEngine, subject: Subject) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(delete(Subject).where(Subject.id == subject.id))
        await session.commit()


# --- one answer, submitted twice at once (S34) ------------------------------------------------


async def test_two_simultaneous_submissions_of_one_attempt_record_one_observation(
    live_client: AsyncClient, engine: AsyncEngine
) -> None:
    """The failure this exists for is a double-click, or a retry after a dropped response.

    Sequentially this already worked — the second submission reads the first one's row. The
    open question S58 records is what happens when neither has committed when the other looks,
    which is the only version of this that a shared-session test cannot ask.
    """
    subject, kc = await _seed_kc(engine)
    try:
        created = await live_client.post(
            f"{API}/items",
            json={
                "item_type": "mcq",
                "stem": "Which wins?",
                "kcs": [{"kc_id": str(kc.id)}],
                "answer_key": {"choices": ["a", "b"], "correct": 1},
                "difficulty": 0.0,
            },
        )
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]

        body = {"response": {"choice": 1}, "attempt_id": str(uuid.uuid4())}
        first, second = await asyncio.gather(
            live_client.post(f"{API}/items/{item_id}/answer", json=body),
            live_client.post(f"{API}/items/{item_id}/answer", json=body),
        )

        # Both may legitimately succeed — one grades, the other replays the stored grade. What
        # must not happen is two observations, which would move mastery twice for one answer.
        assert {first.status_code, second.status_code} <= {200, 201, 409}
        async with AsyncSession(engine) as session:
            events = await session.scalar(
                select(func.count()).select_from(LearningEvent).where(LearningEvent.kc_id == kc.id)
            )
        assert events == 1, f"one answer produced {events} observations"
    finally:
        await _drop_subject(engine, subject)


# --- one address, registered twice at once (S21) ----------------------------------------------


async def test_two_simultaneous_registrations_of_one_address_create_one_account(
    live_client: AsyncClient, engine: AsyncEngine
) -> None:
    """`register` checks for the address and then inserts, which is not atomic.

    The unique constraint is what actually decides, and the loser is meant to come back as the
    same 409 a plain duplicate gets. Nothing proved that until there were two connections.
    """
    address = f"race-{uuid.uuid4().hex[:8]}@example.com"
    body = {"email": address, "password": "a sufficiently long password"}
    try:
        first, second = await asyncio.gather(
            live_client.post(f"{API}/auth/register", json=body),
            live_client.post(f"{API}/auth/register", json=body),
        )
        assert sorted([first.status_code, second.status_code]) == [201, 409]

        async with AsyncSession(engine) as session:
            accounts = await session.scalar(
                select(func.count()).select_from(Learner).where(Learner.email == address)
            )
        assert accounts == 1
    finally:
        async with AsyncSession(engine) as session:
            await session.execute(delete(Learner).where(Learner.email == address))
            await session.commit()


# --- one source, claimed by two workers (S37) -------------------------------------------------


async def test_two_workers_claiming_one_source_produce_one_owner(
    engine: AsyncEngine, live_learner: Learner
) -> None:
    """A lease that both workers believe they hold is the same source ingested twice."""
    settings = Settings()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        source = Source(
            learner_id=live_learner.id,
            kind=SourceKind.FILE,
            origin="contested.txt",
            status=SourceStatus.PENDING,
            content_type="text/plain",
            blob_key=f"b/{uuid.uuid4().hex}",
            meta={},
        )
        session.add(source)
        await session.commit()

    async def claim() -> Source | None:
        async with AsyncSession(engine) as session:
            return await ingestion.claim_source(session, source.id, settings=settings)

    first, second = await asyncio.gather(claim(), claim())
    claimed = [result for result in (first, second) if result is not None]
    assert len(claimed) == 1, "both workers believed they held the lease"


# --- one session, two requests (S21) ----------------------------------------------------------


async def test_a_session_resolves_the_same_learner_from_two_connections(
    live_client: AsyncClient, live_learner: Learner
) -> None:
    """The resolver touches `last_used_at` on use, so two requests write the same row at once."""
    first, second = await asyncio.gather(
        live_client.get(f"{API}/auth/me"),
        live_client.get(f"{API}/auth/me"),
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"] == str(live_learner.id)


async def test_revoking_a_session_is_seen_by_a_connection_that_already_used_it(
    live_client: AsyncClient, engine: AsyncEngine, live_learner: Learner
) -> None:
    """Revocation that is only visible to the connection that performed it is not revocation."""
    assert (await live_client.get(f"{API}/auth/me")).status_code == 200

    async with AsyncSession(engine) as session:
        assert await auth.revoke_all(session, live_learner.id) == 1

    assert (await live_client.get(f"{API}/auth/me")).status_code == 401
