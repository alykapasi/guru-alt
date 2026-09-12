"""The learner, and the credential that proves who they are (S21).

``email`` is the login identity. ``handle`` stays the short public identifier — it is what
logs, fixtures and operators read, and it is referenced throughout the suite, so it remains a
first-class column rather than being folded into the address.

Both credential columns are nullable, and together they say how (and whether) this learner
can sign in. Neither set is a learner with no way in at all: every row created before S21 is
in that state, and so is one an operator creates for somebody who has not enrolled yet.
An address with no hash is the shape an externally-authenticated learner would take, so
adding a second way of signing in later does not need this table to change. A hash with no
address is the one combination that is nonsense, and the database refuses it rather than
leaving it to be documented.
"""

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Learner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An end user of the platform."""

    __tablename__ = "learners"
    __table_args__ = (
        CheckConstraint(
            "password_hash IS NULL OR email IS NOT NULL",
            name="ck_learners_password_requires_email",
        ),
    )

    handle: Mapped[str] = mapped_column(unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(default=None)
    # Stored lower-cased (see ``app.services.auth.normalise_email``) so that one address is
    # one account regardless of how it was typed. Unique, and Postgres does not count NULLs
    # as equal, so any number of credential-less learners coexist.
    email: Mapped[str | None] = mapped_column(unique=True, index=True, default=None)
    password_hash: Mapped[str | None] = mapped_column(default=None)
