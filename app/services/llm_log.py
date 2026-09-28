"""Where accounting writes go (CLAUDE.md: token/cost per call, day one).

The records themselves are written by ``app.llm.meter`` for every call the client makes
(S48); this module owns the session they are written on.

**Accounting is not part of the work it pays for.** The row is written on its own session
and committed immediately, so a business transaction that rolls back after a paid call
still leaves the call recorded. The money left regardless of whether the work survived.
A failed write is logged and never allowed to propagate: losing the audit row is bad, but
failing the learner's turn over bookkeeping is worse.

Tests rebind the session factory (see ``tests/conftest.py``) so accounting shares the
test's transaction and rolls back with it.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory

AccountingSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_session_factory: AccountingSessionFactory = SessionFactory


def set_accounting_session_factory(factory: AccountingSessionFactory) -> AccountingSessionFactory:
    """Point accounting at a different session source. Returns the previous one."""
    global _session_factory
    previous = _session_factory
    _session_factory = factory
    return previous


def accounting_session() -> AbstractAsyncContextManager[AsyncSession]:
    """A session for an accounting write, from whichever source is current.

    Public so other ledgers (``app.services.decision_log``) share the same independence from
    the business transaction — and the same test routing — without reaching into this module.
    """
    return _session_factory()
