"""Opt-in gate for the tests that go through a *real* Redis broker (S58).

Job dispatch is exercised offline by ``InMemoryBroker`` — that covers the wiring and nothing
about delivery. What it cannot show is that a message survives being serialised, written to a
Redis list, read back by a different connection and parsed into the same task with the same
arguments. Until now nothing did, which is one of the gaps S58 names.

These are gated the same way the live-model tests are, and for the same reason: a suite whose
behaviour depends on what happens to be running on the developer's machine means something
different on a laptop than in CI. Set ``GURU_QUEUE_TESTS=1`` (CI does), or they skip.
"""

import os
import socket
from urllib.parse import urlparse

QUEUE_ENV_VAR = "GURU_QUEUE_TESTS"
SKIP_REASON = f"queue integration test: set {QUEUE_ENV_VAR}=1 (needs a reachable Redis)"

# Deliberately not database 0. A developer running this against their dev Redis should not have
# the suite's queue land in the same list the dev worker is consuming from.
TEST_REDIS_DB = 15

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get(QUEUE_ENV_VAR, "").strip().lower() in _TRUTHY


def suite_redis_url(url: str) -> str:
    """``url`` pointed at the suite's own database rather than whatever it names.

    Deliberately not named ``test_*``: a helper with that prefix, imported into a test
    module, is collected as a test and fails on a fixture named after its own argument.
    """
    parsed = urlparse(url)
    return parsed._replace(path=f"/{TEST_REDIS_DB}").geturl()


def reachable(url: str, timeout: float = 0.5) -> bool:
    """Whether something is listening. Cheaper than a client handshake and enough to skip on."""
    parsed = urlparse(url)
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((parsed.hostname or "localhost", parsed.port or 6379))
        return True
    except OSError:
        return False
    finally:
        sock.close()
