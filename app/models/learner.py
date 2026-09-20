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

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, false
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
    # The only authorization *tier* there is (P10). Not a role table and deliberately not one:
    # two tiers is what the product has, and a table of roles nobody assigns is a permission
    # model that exists only in the schema. It lives on the learner rather than beside them
    # because there is no such thing as an administrator who is not also an account — the
    # portal is read with the same session everything else is.
    is_admin: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # The provider's id for this person (S21). NULL for a learner nobody has signed in as yet:
    # an account created by an import before its owner arrives, or the dev learner. Unique,
    # because one provider identity resolving to two learners is one sign-in with two answers.
    auth_subject: Mapped[str | None] = mapped_column(unique=True, index=True, default=None)
    # Set by an administrator (S21). Access stops immediately and the reason is in
    # ``account_actions``; the learner's work is untouched, because suspension is not deletion.
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
