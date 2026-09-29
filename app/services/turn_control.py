"""Stopping a turn, and bounding how long it runs (S47).

A turn used to run until its flow finished, however long that took: the provider SDKs bound
each network *read*, not a call, and a turn makes several calls. And a learner could not stop a
reply — only close the page, which records the turn as ``cancelled`` and loses the text.

**One cancellation path for both.** :meth:`TurnControl.run` awaits each step of the flow inside
``asyncio.timeout_at(deadline)``. A Stop reschedules that same timeout to *now*. Either way the
flow is cancelled at its current await, in the turn's own task (so context variables and the
request's session behave exactly as they do for a disconnect), its ``finally`` blocks close the
provider stream — which the meter already settles as ``partial`` — and ``run`` yields one
:class:`Interrupted` marker. The caller decides what that means for the transcript.

**How a Stop arrives.** In this process, through ``_local``. From another process, as a
Postgres notification on channel ``turn_stop`` whose payload is the turn id — delivered on the
connection the turn's lock already holds (``TurnClaim.connection``), so only a turn that is
really running is listening. The payload is an id, never learner text.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Self

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.turn_lock import TurnClaim

log = structlog.get_logger(__name__)

CHANNEL = "turn_stop"


class Interrupted(StrEnum):
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"


class TurnControl:
    def __init__(self, turn_id: uuid.UUID, *, deadline_s: float) -> None:
        self.turn_id = turn_id
        self.deadline_s = deadline_s
        self.stopped = False
        self._timeout: asyncio.Timeout | None = None
        self._unlisten: Callable[[], Awaitable[None]] | None = None

    async def __aenter__(self) -> Self:
        _local[self.turn_id] = self
        return self

    async def __aexit__(self, *exc: object) -> None:
        _local.pop(self.turn_id, None)
        if self._unlisten is not None:
            await self._unlisten()

    def stop(self) -> None:
        """Stop at the current step, or at the next one if none is in flight. Idempotent."""
        self.stopped = True
        if self._timeout is not None and not self._timeout.expired():
            self._timeout.reschedule(asyncio.get_running_loop().time())

    async def listen(self, claim: TurnClaim) -> None:
        """Take Stops from other processes on the claim's own connection.

        Without a connection (the lock fell back to in-process) only in-process Stops arrive —
        the same single-process degradation the lock has. A failure to listen leaves the turn
        running without Stop; its deadline still holds.
        """
        if claim.connection is None:
            return
        try:
            raw = await claim.connection.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None

            def on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
                if payload == str(self.turn_id):
                    self.stop()

            await driver.add_listener(CHANNEL, on_notify)

            async def unlisten() -> None:
                try:
                    await driver.remove_listener(CHANNEL, on_notify)
                except Exception:  # the connection may already be closing with the claim
                    pass

            self._unlisten = unlisten
        except Exception as exc:
            log.warning("turn_control.listen_failed", turn_id=str(self.turn_id), error=str(exc))

    async def run[T](self, events: AsyncGenerator[T]) -> AsyncIterator[T | Interrupted]:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.deadline_s
        try:
            while True:
                # Checked before each step, not only by the timeout: a step that never suspends
                # returns its event before a rescheduled timeout can fire.
                if self.stopped or loop.time() >= end:
                    yield Interrupted.STOPPED if self.stopped else Interrupted.TIMED_OUT
                    return
                timeout = asyncio.timeout_at(end)
                try:
                    async with timeout:
                        self._timeout = timeout
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if not timeout.expired():
                        raise  # the flow's own timeout, not ours
                    yield Interrupted.STOPPED if self.stopped else Interrupted.TIMED_OUT
                    return
                finally:
                    self._timeout = None
                yield event
        finally:
            await events.aclose()


# Turns running in this process, by id. A Stop for one of these never needs the database.
_local: dict[uuid.UUID, TurnControl] = {}


async def request_stop(engine: AsyncEngine, turn_id: uuid.UUID) -> None:
    """Ask the running turn ``turn_id`` to stop, wherever it runs."""
    local = _local.get(turn_id)
    if local is not None:
        local.stop()
        return
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": str(turn_id)},
        )
