"""One turn at a time per conversation, across every process (S17, S34).

Two turns running on the same conversation corrupt each other in ways the learner sees: both
read the history before either appends to it, so messages interleave; and both can resume the
*same* paused graph, which grades one practice answer twice and writes two mastery observations
for one piece of work.

The claim used to be a set in one process's memory, and that was the right size for what it
guarded: the graphs compiled with ``InMemorySaver``, so a paused turn could only ever be resumed
by the process that paused it, and no second process could reach the state to corrupt. Now that
the checkpointer is durable (``app/agent/checkpointing.py``), that is no longer true — any worker
can resume any paused practice — so the in-process claim would be a lock that stopped guarding
the thing it exists for, precisely when the thing became reachable.

**A Postgres session-level advisory lock, on a connection held for the turn.** Not a lease row,
and the difference is the failure mode. A lease needs a duration, and a turn's duration is
whatever the model and the learner take — so any timeout is either long enough to strand a
conversation after a crash, or short enough to hand a live turn to a second claimant. An
advisory lock held on a dedicated connection needs no timeout at all: Postgres releases it when
the connection ends, and a crashed process's connections end. The cost is one connection per
turn in flight, which is bounded by the same thing the SSE responses already bound.

Claims fall back to a process-local set when the lock cannot be taken out at all (no database
reachable) — the same degradation as the checkpointer's, for the same reason: refusing to serve
chat is worse than serving it with the guarantee it had last week, as long as nothing pretends
otherwise.

An overlapping turn is refused rather than queued. Queuing would hold an SSE connection open
behind work the second turn's history read has already gone stale on; refusing is the defined
behavior, and the one a chat client can act on.
"""

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

log = structlog.get_logger(__name__)

_LOCK_NAMESPACE = 0x47555255
"""The high half of every advisory-lock key ("GURU"), so these cannot collide with an advisory
lock taken by anything else against the same database."""

_fallback: set[uuid.UUID] = set()
"""Conversations claimed without a database. Mutated only between awaits, so the check-then-add
is atomic on the event loop."""


def lock_key(conversation_id: uuid.UUID) -> int:
    """The conversation's advisory-lock object id: the low 32 bits of its UUID, unsigned.

    Two 32-bit halves (namespace, object) rather than one 64-bit key so ``pg_locks`` can be
    read back by column without reassembling the pair — which is what makes :func:`is_active`
    a plain query instead of an acquisition attempt that would itself take the lock it is
    asking about.

    Truncating to 32 bits means two conversations collide if they share those bits. The cost of
    a collision is one conversation's turn being refused while another's runs — which the client
    already handles, because that is what an overlapping turn looks like — never two turns being
    confused for each other.
    """
    return conversation_id.int & 0xFFFFFFFF


def _as_int4(value: int) -> int:
    """``value`` as Postgres sees an ``int4``.

    ``pg_try_advisory_lock`` takes signed ``int4`` and would reject anything above 2^31 - 1, while
    ``pg_locks`` exposes the same key as an unsigned ``oid``. Both are the same 32 bits; only
    the spelling differs, and mixing the two silently makes :func:`is_active` never find a lock
    for half of all conversations.
    """
    return value - (1 << 32) if value >= (1 << 31) else value


@dataclass
class TurnClaim:
    """A held claim on a conversation. :meth:`release` is idempotent."""

    conversation_id: uuid.UUID
    connection: AsyncConnection | None = None
    _released: bool = field(default=False, repr=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.connection is None:
            _fallback.discard(self.conversation_id)
            return
        # Closing the connection releases the advisory lock, so the explicit unlock is
        # belt-and-braces — but it returns the lock before the connection unwinds, which
        # matters when a client retries immediately after a refused turn.
        try:
            await self.connection.execute(
                text("SELECT pg_advisory_unlock(:classid, :objid)"),
                {
                    "classid": _as_int4(_LOCK_NAMESPACE),
                    "objid": _as_int4(lock_key(self.conversation_id)),
                },
            )
        except Exception:
            log.warning("turn_lock.unlock_failed", conversation_id=str(self.conversation_id))
        finally:
            await self.connection.close()


async def claim(engine: AsyncEngine, conversation_id: uuid.UUID) -> TurnClaim | None:
    """Reserve this conversation for a turn, or ``None`` if one is already running.

    ``engine`` rather than an imported one: the lock is only a lock if it is taken on the
    same database backend the rest of the request is using (see ``deps.get_engine``).
    """
    try:
        # AUTOCOMMIT because this connection is held for the whole turn: without it the first
        # statement opens a transaction that then sits idle for the length of an SSE stream,
        # pinning a snapshot and blocking vacuum for as long as the learner takes to reply.
        connection = await engine.connect()
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
    except Exception as exc:
        log.error("turn_lock.no_connection", error=str(exc))
        if conversation_id in _fallback:
            return None
        _fallback.add(conversation_id)
        return TurnClaim(conversation_id=conversation_id)
    try:
        taken = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": _as_int4(_LOCK_NAMESPACE), "objid": _as_int4(lock_key(conversation_id))},
        )
    except Exception as exc:
        await connection.close()
        log.error("turn_lock.claim_failed", error=str(exc))
        return None
    if not taken:
        await connection.close()
        return None
    return TurnClaim(conversation_id=conversation_id, connection=connection)


async def is_active(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Whether any process currently holds this conversation.

    Read from ``pg_locks`` rather than by trying to acquire: this is called on the read path
    (``turn.reap_stale``) to decide whether a ``PENDING`` turn is dead or merely slow, and a
    liveness check that acquires the lock it is asking about would report every dead turn as
    alive exactly once, then release it. Reading takes no lock, so unlike :func:`claim` it can
    and should run on the request's own session.
    """
    if conversation_id in _fallback:
        return True
    try:
        held = await session.scalar(
            text(
                "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = :classid AND objid = :objid AND granted"
            ),
            # Unsigned here: pg_locks exposes the key as an oid. See _as_int4.
            {"classid": _LOCK_NAMESPACE, "objid": lock_key(conversation_id)},
        )
    except Exception as exc:
        # Unknown, so say "active": reaping marks a turn CANCELLED, and reporting a running
        # turn as abandoned is the more damaging of the two possible errors.
        log.error("turn_lock.liveness_unknown", error=str(exc))
        return True
    return held is not None
