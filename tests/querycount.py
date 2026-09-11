"""Counting the SQL one operation issues, so a query budget can be asserted (S62).

Costs that grow with a learner's history are invisible in a test suite whose fixtures are
three rows deep — the operation returns the right answer either way, and only a real account
notices. A query count is the part of that cost that can be measured deterministically: no
clock, no warm cache, no machine to compare against. It is not latency, but an operation whose
query count grows with the graph will not be fixed by a faster database.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

_TRANSACTION_CONTROL = ("SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN", "COMMIT")


@dataclass
class QueryCount:
    statements: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.statements)

    def __repr__(self) -> str:  # what a budget failure should print
        return f"QueryCount({len(self.statements)}):\n" + "\n".join(
            f"  {s.split(chr(10))[0][:110]}" for s in self.statements
        )


@contextmanager
def count_queries(session: AsyncSession) -> Iterator[QueryCount]:
    """Count every statement executed on ``session``'s connection inside the block."""
    counted = QueryCount()
    sync_engine = session.get_bind().engine

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        # Transaction control is the harness's, not the operation's: the suite runs each test
        # inside a savepoint, and counting those would put a fixed offset on every budget.
        if not statement.lstrip().upper().startswith(_TRANSACTION_CONTROL):
            counted.statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _on_execute)
    try:
        yield counted
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_execute)
