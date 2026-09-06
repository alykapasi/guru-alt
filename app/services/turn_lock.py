"""One turn at a time per conversation.

Two turns running on the same conversation corrupt each other in ways the learner sees: both
read the history before either appends to it, so messages interleave; and both can resume the
*same* paused graph, which grades one practice answer twice and writes two mastery
observations for one piece of work.

The claim is in-process, which is deliberately exactly as strong as the guarantee the graphs
already rely on: ``app/agent/workflow.py`` and ``app/agent/refinement.py`` compile with
``InMemorySaver``, so a paused turn can only ever be resumed by the process that paused it.
Making this claim durable (a row, or an advisory lock on a held connection) buys nothing until
those checkpointers are durable too — and at that point both should move together.

An overlapping turn is refused rather than queued. Queuing would hold an SSE connection open
behind work the second turn's history read has already gone stale on; refusing is the defined
behavior, and the one a chat client can act on.
"""

import uuid

_active: set[uuid.UUID] = set()
"""Conversations with a turn in flight. Mutated only between awaits, so the check-then-add
below is atomic on the event loop."""


def claim(conversation_id: uuid.UUID) -> bool:
    """Reserve this conversation for a turn. ``False`` if one is already running."""
    if conversation_id in _active:
        return False
    _active.add(conversation_id)
    return True


def release(conversation_id: uuid.UUID) -> None:
    """Release the claim. Safe to call for a conversation that holds none."""
    _active.discard(conversation_id)


def is_active(conversation_id: uuid.UUID) -> bool:
    """Whether a turn currently holds this conversation."""
    return conversation_id in _active
