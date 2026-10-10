"""Counting the SQL one operation issues, so a query budget can be asserted (S62).

Costs that grow with a learner's history are invisible in a test suite whose fixtures are
three rows deep — the operation returns the right answer either way, and only a real account
notices. A query count is the part of that cost that can be measured deterministically: no
clock, no warm cache, no machine to compare against. It is not latency, but an operation whose
query count grows with the graph will not be fixed by a faster database. Rows are counted too:
an unbounded list is one query whatever its size, so only its row count shows it growing.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

_TRANSACTION_CONTROL = ("SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN", "COMMIT")


@dataclass
class QueryCount:
    entries: list[tuple[str, int]] = field(default_factory=list)

    @property
    def statements(self) -> list[str]:
        return [s for s, _ in self.entries]

    @property
    def rows(self) -> int:
        """Rows returned or affected, summed. A row count grows where a statement count does
        not: an unbounded list is one query whatever its size."""
        return sum(max(n, 0) for _, n in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:  # what a budget failure should print
        return f"QueryCount({len(self.entries)} statements, {self.rows} rows):\n" + "\n".join(
            f"  [{n:>6}] {s.split(chr(10))[0][:110]}" for s, n in self.entries
        )


@contextmanager
def count_queries(session: AsyncSession) -> Iterator[QueryCount]:
    """Count every statement executed on ``session``'s connection inside the block, with the
    rows each returned (``cursor.rowcount``, which asyncpg reports for SELECTs too)."""
    counted = QueryCount()
    sync_engine = session.get_bind().engine

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        # Transaction control is the harness's, not the operation's: the suite runs each test
        # inside a savepoint, and counting those would put a fixed offset on every budget.
        if not statement.lstrip().upper().startswith(_TRANSACTION_CONTROL):
            counted.entries.append((statement, cursor.rowcount))

    event.listen(sync_engine, "after_cursor_execute", _on_execute)
    try:
        yield counted
    finally:
        event.remove(sync_engine, "after_cursor_execute", _on_execute)
