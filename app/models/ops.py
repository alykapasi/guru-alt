"""Operational state the application keeps about itself.

Currently one table: the record of when an alert condition started and stopped firing. Alerts
are evaluated on demand (``app.core.alerts``), which answers "is anything wrong *now*" and
cannot answer "was anything wrong at three in the morning" — so a condition could fire, resolve,
and leave nobody any the wiser, which was the honest gap in the alerting entry.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Identity, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import UUIDPrimaryKeyMixin


class AlertTransition(UUIDPrimaryKeyMixin, Base):
    """One change of state for one alert condition: it started firing, or it stopped.

    **Transitions, not evaluations.** Recording every poll would write a row a minute forever
    and bury the two rows anybody wants; recording only the changes means the table's length is
    the number of things that actually happened. The current state of a condition is the
    `firing` flag on its most recent row, and a condition with no rows has never fired.

    ``detail`` and ``action`` are copied rather than joined to: they are what the alert *said at
    the time*, and a threshold that has since been retuned would otherwise silently rewrite the
    history of every incident it was involved in.
    """

    __tablename__ = "alert_transitions"
    __table_args__ = (
        # The read this table exists for: the latest row per condition, and a window of recent
        # changes. Both walk newest-first within a name, and both order by `seq`.
        Index("ix_alert_transitions_name_seq", "name", "seq"),
    )

    # A database sequence, not the clock. `created_at` defaults to `now()`, which in Postgres
    # is the *transaction* timestamp — every row written by one sweep carries the same value,
    # so ordering by it cannot separate two transitions recorded together and falls through to
    # a random UUID. That tie has been the same defect three times already in this codebase
    # (S56, S14, S62); here it would let `current_states` report a condition resolved when the
    # last thing that happened was it firing. `seq` is monotonic by construction and needs no
    # tiebreak.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    # No index of its own: the composite above leads on `name`, and Postgres uses a composite
    # index for a prefix of its columns, so a second single-column index would be a write cost
    # with no read it is the cheapest answer to.
    name: Mapped[str]
    firing: Mapped[bool]
    severity: Mapped[str | None] = mapped_column(default=None)
    detail: Mapped[str | None] = mapped_column(default=None)
    action: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "firing" if self.firing else "resolved"
        return f"<AlertTransition {self.name} {state}>"


__all__ = ["AlertTransition"]
