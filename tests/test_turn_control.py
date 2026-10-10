"""Stopping a turn and bounding how long it runs (S47)."""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services import turn_control, turn_lock
from app.services.turn_control import Interrupted, TurnControl


class _Flow:
    """A flow that yields tokens, then waits; records whether it was closed."""

    def __init__(self, tokens: list[str], *, then_wait: float = 30.0, on_step=None) -> None:
        self.tokens, self.then_wait, self.on_step = tokens, then_wait, on_step
        self.closed = False

    async def events(self) -> AsyncGenerator[str]:
        try:
            for t in self.tokens:
                if self.on_step:
                    self.on_step()
                yield t
            await asyncio.sleep(self.then_wait)
            yield "late"
        finally:
            self.closed = True


async def _collect(control: TurnControl, flow: _Flow) -> list:
    return [e async for e in control.run(flow.events())]


async def test_events_pass_through_when_nothing_interrupts() -> None:
    flow = _Flow(["a", "b"], then_wait=0)
    async with TurnControl(uuid.uuid4(), deadline_s=5.0) as control:
        assert await _collect(control, flow) == ["a", "b", "late"]


async def test_a_stop_mid_stream_closes_the_flow_and_says_stopped() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=5.0) as control:
        flow = _Flow(["a"])
        out = []
        async for e in control.run(flow.events()):
            out.append(e)
            if e == "a":
                control.stop()
    assert out == ["a", Interrupted.STOPPED]
    assert flow.closed


async def test_a_stop_between_steps_stops_at_the_next_one() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=5.0) as control:
        flow = _Flow(["a", "b"])
        out = []
        async for e in control.run(flow.events()):
            out.append(e)
            control.stop()  # while the consumer holds the event, outside any awaited step
    assert out == ["a", Interrupted.STOPPED]


async def test_a_stop_before_any_text() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=5.0) as control:
        control.stop()
        assert await _collect(control, _Flow(["a"])) == [Interrupted.STOPPED]


async def test_the_deadline_cuts_a_slow_flow_off() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=0.05) as control:
        flow = _Flow(["a"])
        assert await _collect(control, flow) == ["a", Interrupted.TIMED_OUT]
    assert flow.closed


async def test_a_flows_own_timeout_is_not_the_deadline() -> None:
    async def raises() -> AsyncGenerator[str]:
        yield "a"
        raise TimeoutError("the flow's own")

    async with TurnControl(uuid.uuid4(), deadline_s=5.0) as control:
        with pytest.raises(TimeoutError, match="the flow's own"):
            await _collect_gen(control, raises())


async def _collect_gen(control: TurnControl, gen: AsyncGenerator[str]) -> list:
    return [e async for e in control.run(gen)]


async def test_request_stop_reaches_a_turn_in_this_process(engine: AsyncEngine) -> None:
    turn_id = uuid.uuid4()
    async with TurnControl(turn_id, deadline_s=5.0) as control:
        await turn_control.request_stop(engine, turn_id)
        assert control.stopped


async def test_a_notification_from_another_connection_stops_a_listening_turn(
    engine: AsyncEngine,
) -> None:
    """The cross-process path: the turn listens on its claim's connection; the stop arrives
    as a NOTIFY sent over a different connection."""
    claim = await turn_lock.claim(engine, uuid.uuid4())
    assert claim is not None and claim.connection is not None
    turn_id = uuid.uuid4()
    try:
        async with TurnControl(turn_id, deadline_s=5.0) as control:
            await control.listen(claim)
            async with engine.connect() as other:
                other = await other.execution_options(isolation_level="AUTOCOMMIT")
                await other.execute(
                    text("SELECT pg_notify(:c, :p)"), {"c": turn_control.CHANNEL, "p": str(turn_id)}
                )
            for _ in range(50):
                if control.stopped:
                    break
                await asyncio.sleep(0.02)
            assert control.stopped
    finally:
        await claim.release()


async def test_a_notification_for_another_turn_is_ignored(engine: AsyncEngine) -> None:
    claim = await turn_lock.claim(engine, uuid.uuid4())
    assert claim is not None
    try:
        async with TurnControl(uuid.uuid4(), deadline_s=5.0) as control:
            await control.listen(claim)
            async with engine.connect() as other:
                other = await other.execution_options(isolation_level="AUTOCOMMIT")
                await other.execute(
                    text("SELECT pg_notify(:c, :p)"),
                    {"c": turn_control.CHANNEL, "p": str(uuid.uuid4())},
                )
            await asyncio.sleep(0.2)
            assert not control.stopped
    finally:
        await claim.release()


async def test_the_registry_is_empty_after_the_turn() -> None:
    turn_id = uuid.uuid4()
    async with TurnControl(turn_id, deadline_s=5.0):
        assert turn_id in turn_control._local
    assert turn_id not in turn_control._local
