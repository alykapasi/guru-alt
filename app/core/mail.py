"""Where an out-of-band message would go, if there were anywhere to send it (S21).

A password reset is a token plus a way to deliver it, and this repository has the first and not
the second. That is stated here rather than worked around, because the two available ways of
working around it are both worse than the gap:

* Returning the token from the HTTP response turns "I forgot my password" into "give me a
  password reset for any address I can name", which is not a weaker reset, it is no reset.
* Pretending an SMTP client exists and failing at runtime would make the *first* real
  deployment discover it, during an incident, while somebody is locked out.

So delivery is a seam with one honest implementation. ``LoggingMailer`` writes the message to
the application log, which is genuinely useful in development — the token is there, the flow is
exercisable end to end — and is genuinely not a production mailer, which is why
``app.core.release`` refuses to start a production instance with password reset enabled while
this is the only transport configured.

Adding a real one is a small, well-bounded job: implement ``Mailer.send`` against whatever
transport the deployment has, and register it. Nothing above this module needs to change.
"""

from typing import Protocol, runtime_checkable

import structlog

log = structlog.get_logger(__name__)


@runtime_checkable
class Mailer(Protocol):
    """Sends one message to one address. Deliberately the smallest possible surface."""

    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class LoggingMailer:
    """Writes the message to the log instead of sending it. Development only.

    The address is logged and the body is logged, because in development the body *is* the
    delivery — a reset link nobody can read is a flow nobody can test. That is also exactly
    why this must not run in production: it writes a credential to the log.
    """

    production_safe = False

    async def send(self, *, to: str, subject: str, body: str) -> None:
        log.warning("mail.not_sent", to=to, subject=subject, body=body)


def build_mailer() -> Mailer:
    """The transport this deployment has. One option, and the release gate knows it."""
    return LoggingMailer()
