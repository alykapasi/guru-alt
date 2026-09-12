"""A paused conversation that survives the process that paused it (S17).

Two graphs stop mid-conversation and wait for the learner: the goal-refinement gate, and the
guided-practice loop. Both compiled against ``InMemorySaver``, so everything between the
interrupt and its resume lived in one process's heap. A deploy, a crash, or an autoscaler moving
the pod silently discarded every paused conversation in flight — the learner saw a practice
question they could no longer answer, and the gate's dispatcher degraded that to "start plain
chat instead", which is a reasonable thing to do with state that is genuinely gone and a
terrible thing to need.

The central test here is ``test_a_paused_graph_resumes_in_a_process_that_never_saw_it``: a graph
paused through one saver, read back through a *different* saver on a different pool. That is as
close to a restart as a test can get without one, and it is the claim the item is about.

Two other things had to move with it, and both are here for a reason that is easy to state
backwards. Making the state durable without them would have been *worse* than leaving it
volatile: an in-process turn lock in front of shared state lets two workers resume the same
paused practice and grade one answer twice, and a negotiation whose ownership record died with
the process can only ever be refused, which discards it exactly as surely as losing it did.
"""

import uuid
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent import checkpointing
from app.core.config import get_settings
from app.core.readiness import readiness
from app.models.learner import Learner
from app.services import onboarding_sessions, turn_lock
from app.storage.memory import InMemoryBlobStore


class _State(TypedDict):
    value: str
    answer: str


def _paused_graph(saver: Any) -> Any:
    """A graph that runs one node, interrupts, and stores whatever it is resumed with."""

    async def start(state: _State) -> dict[str, Any]:
        return {"value": state["value"]}

    async def wait(state: _State) -> dict[str, Any]:
        return {"answer": interrupt({"prompt": state["value"]})}

    graph = StateGraph(_State)  # ty: ignore[invalid-argument-type]
    graph.add_node("start", start)
    graph.add_node("wait", wait)
    graph.add_edge(START, "start")
    graph.add_edge("start", "wait")
    graph.add_edge("wait", END)
    return graph.compile(checkpointer=saver)


async def _durable_saver() -> Any:
    """A Postgres saver on its own pool, as a fresh process would build one."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=checkpointing.psycopg_dsn(get_settings().database_url),
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    # The row factory is set in `kwargs` above, which the pool's own generic parameter does
    # not track — the same reason app/agent/checkpointing.py silences this.
    saver = AsyncPostgresSaver(pool)  # ty: ignore[invalid-argument-type]
    await saver.setup()
    return saver, pool


async def _forget_thread(thread_id: str) -> None:
    """Drop one thread's checkpoint rows: this suite writes outside the test transaction."""
    _saver, pool = await _durable_saver()
    try:
        async with pool.connection() as connection:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await connection.execute(
                    f"DELETE FROM {table} WHERE thread_id = %s",
                    (thread_id,),
                )
    finally:
        await pool.close()


# --- the claim ----------------------------------------------------------------


async def test_a_paused_graph_resumes_in_a_process_that_never_saw_it() -> None:
    """The whole of S17 in one test. The saver that resumes the graph is a different object on
    a different connection pool from the one that paused it — which is what a restart, a second
    worker, and a rolling deploy all look like from the state's point of view."""
    thread_id = f"test-{uuid.uuid4().hex}"
    config: Any = {"configurable": {"thread_id": thread_id}}

    paused_saver, paused_pool = await _durable_saver()
    try:
        await _paused_graph(paused_saver).ainvoke(
            {"value": "what is velocity?", "answer": ""}, config
        )
    finally:
        await paused_pool.close()  # the process that was serving the learner is gone

    resumed_saver, resumed_pool = await _durable_saver()
    try:
        graph = _paused_graph(resumed_saver)
        snapshot = await graph.aget_state(config)
        assert snapshot.next, "the graph should still be waiting for the learner"
        assert snapshot.values["value"] == "what is velocity?"

        final = await graph.ainvoke(Command(resume="speed with direction"), config)
        assert final["answer"] == "speed with direction"
    finally:
        await resumed_pool.close()
    await _forget_thread(thread_id)


async def test_the_same_state_in_memory_does_not_survive() -> None:
    """The before picture, so the test above is measuring something. An ``InMemorySaver`` in a
    second process knows nothing about the first one's paused conversation."""
    thread_id = f"test-{uuid.uuid4().hex}"
    config: Any = {"configurable": {"thread_id": thread_id}}

    await _paused_graph(InMemorySaver()).ainvoke({"value": "q", "answer": ""}, config)

    snapshot = await _paused_graph(InMemorySaver()).aget_state(config)
    assert not snapshot.next
    assert snapshot.values == {}


# --- wiring -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+asyncpg://u:p@h:5433/db", "postgresql://u:p@h:5433/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql://u:p@h/db"),
        ("postgres://u:p@h/db", "postgres://u:p@h/db"),
    ],
)
def test_only_the_driver_is_stripped_from_the_dsn(url: str, expected: str) -> None:
    """Everything after the scheme is left alone: a checkpointer pointed at a different host,
    port or database from the rest of the app would store paused conversations somewhere no
    other instance reads, which looks exactly like durability until a restart."""
    assert checkpointing.psycopg_dsn(url) == expected


def test_a_malformed_url_is_passed_through_rather_than_mangled() -> None:
    assert checkpointing.psycopg_dsn("not-a-url") == "not-a-url"


async def test_starting_and_stopping_reports_what_it_achieved() -> None:
    await checkpointing.stop()  # whatever an earlier test left
    assert checkpointing.is_durable() is False
    assert isinstance(checkpointing.checkpointer(), InMemorySaver)
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is True
        assert not isinstance(checkpointing.checkpointer(), InMemorySaver)
    finally:
        await checkpointing.stop()
    assert checkpointing.is_durable() is False


async def test_an_unreachable_database_degrades_loudly_rather_than_refusing_to_serve() -> None:
    """Chat, grading and planning do not need the checkpointer, so a pool that cannot open must
    not take the process down — but the fallback has to be visible, because silently restoring
    the old behaviour is how a durability guarantee becomes a rumour."""
    await checkpointing.stop()
    broken = get_settings().model_copy(
        update={"database_url": "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing"}
    )
    try:
        await checkpointing.start(broken)
        assert checkpointing.is_durable() is False
        assert isinstance(checkpointing.checkpointer(), InMemorySaver)
    finally:
        await checkpointing.stop()


async def test_readiness_reports_durability_without_withholding_traffic(
    db_session: AsyncSession,
) -> None:
    """Both halves matter. A volatile checkpointer must be *visible*, because silently
    restoring the old behaviour is how a durability guarantee becomes a rumour — and it must
    not make the instance unready, because chat, grading and planning are all unaffected and
    taking the pod out of rotation would turn a partial degradation into an outage."""
    store = InMemoryBlobStore()

    await checkpointing.stop()
    volatile = await readiness(db_session, store)
    assert volatile.durable_checkpoints is False
    assert volatile.ready is True  # still serving

    try:
        await checkpointing.start(get_settings())
        durable = await readiness(db_session, store)
        assert durable.durable_checkpoints is True
        assert durable.ready is True
    finally:
        await checkpointing.stop()


# --- the lock that had to move with it ----------------------------------------


async def test_a_second_process_cannot_claim_a_conversation_someone_else_holds(
    engine: AsyncEngine,
) -> None:
    """With durable checkpoints, two workers can reach the same paused practice. Without a
    lock they can both resume it, grade one answer twice, and write two mastery observations
    for one piece of work."""
    conversation_id = uuid.uuid4()
    first = await turn_lock.claim(engine, conversation_id)
    assert first is not None
    try:
        assert await turn_lock.claim(engine, conversation_id) is None
    finally:
        await first.release()
    # released, so the next request gets it
    second = await turn_lock.claim(engine, conversation_id)
    assert second is not None
    await second.release()


async def test_releasing_twice_is_harmless(engine: AsyncEngine) -> None:
    """Release runs from a ``finally`` on a path that can also have released already."""
    claim = await turn_lock.claim(engine, uuid.uuid4())
    assert claim is not None
    await claim.release()
    await claim.release()


async def test_a_held_claim_is_visible_to_a_different_connection(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Liveness is what tells ``reap_stale`` that a PENDING turn is running rather than dead.
    Read from the wrong process it used to say "dead" for every other worker's live turn."""
    conversation_id = uuid.uuid4()
    assert await turn_lock.is_active(db_session, conversation_id) is False
    claim = await turn_lock.claim(engine, conversation_id)
    assert claim is not None
    try:
        assert await turn_lock.is_active(db_session, conversation_id) is True
    finally:
        await claim.release()
    assert await turn_lock.is_active(db_session, conversation_id) is False


@pytest.mark.parametrize(
    "conversation_id",
    [
        uuid.UUID(int=0),
        uuid.UUID(int=(1 << 31)),  # the low half lands exactly on the sign boundary
        uuid.UUID(int=0xFFFFFFFF),  # and at the top of the unsigned range
        uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
    ],
)
async def test_a_claim_is_visible_for_every_shape_of_key(
    conversation_id: uuid.UUID, engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """``pg_try_advisory_lock`` takes a signed int4 and ``pg_locks`` reports an unsigned oid.
    Mixing the two makes liveness silently blind for half of all conversations — which reads as
    "every turn on those conversations is abandoned", and reaps live ones."""
    claim = await turn_lock.claim(engine, conversation_id)
    assert claim is not None
    try:
        assert await turn_lock.is_active(db_session, conversation_id) is True
    finally:
        await claim.release()


async def test_a_claim_is_per_conversation(engine: AsyncEngine) -> None:
    one = await turn_lock.claim(engine, uuid.uuid4())
    two = await turn_lock.claim(engine, uuid.uuid4())
    assert one is not None and two is not None
    await one.release()
    await two.release()


async def test_a_claim_does_not_sit_in_an_open_transaction(
    engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The connection is held for the whole turn. Left in a transaction it pins a snapshot and
    blocks vacuum for as long as the learner takes to answer."""
    claim = await turn_lock.claim(engine, uuid.uuid4())
    assert claim is not None
    try:
        idle_in_transaction = await db_session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'idle in transaction' AND application_name = current_setting("
                "'application_name')"
            )
        )
        assert idle_in_transaction == 0
    finally:
        await claim.release()


# --- the ownership record that had to move with it ----------------------------


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"ds-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_an_issued_session_is_recognised_by_its_owner(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)

    found = await onboarding_sessions.require(db_session, record.session_id, learner.id)
    assert found.session_id == record.session_id


async def test_another_learners_session_is_refused(db_session: AsyncSession) -> None:
    """The reason the record exists at all: the id used to be enough on its own."""
    owner = await _learner(db_session)
    intruder = await _learner(db_session)
    record = await onboarding_sessions.issue(db_session, owner.id)

    with pytest.raises(onboarding_sessions.NotYourSession):
        await onboarding_sessions.require(db_session, record.session_id, intruder.id)


async def test_a_forgotten_session_is_refused(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    await onboarding_sessions.forget(db_session, record.session_id)

    with pytest.raises(onboarding_sessions.NotYourSession):
        await onboarding_sessions.require(db_session, record.session_id, learner.id)


async def test_the_same_id_under_two_learners_addresses_two_negotiations() -> None:
    """The half that holds even with no record at all — after a restore from a backup taken
    before the id was issued, say. Namespacing means a leaked id is worth nothing on its own."""
    shared = uuid.uuid4().hex
    a, b = uuid.UUID(int=7), uuid.UUID(int=8)
    assert onboarding_sessions.thread_key(shared, a) != onboarding_sessions.thread_key(shared, b)
