"""What a provider's error means to a learner (S49).

Each adapter reduces its own SDK's exception to a status, an error type, headers or "could
not connect"; this decides what those mean, the same way for every provider. It imports no
SDK. Only errors that survived the SDK's own retries ever get here.
"""

import contextlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.core.config import get_settings
from app.llm.meter import ProviderUnavailable

# Error types a provider names in a body — Anthropic's, inside a stream, arrive on a 200.
BUSY_TYPES = frozenset({"rate_limit_error"})
DOWN_TYPES = frozenset({"overloaded_error", "api_error"})


def retry_after_seconds(headers: Mapping[str, str] | None) -> float:
    """The provider's ``retry-after`` in seconds (a number or an HTTP-date), else the default."""
    default = get_settings().provider_retry_after_seconds
    raw = (headers or {}).get("retry-after")
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def classify(
    *,
    status: int | None = None,
    error_type: str | None = None,
    headers: Mapping[str, str] | None = None,
    unreachable: bool = False,
) -> ProviderUnavailable | None:
    """Busy, down, or ``None`` — not a refusal (a bad request, a bad key: ours to fix)."""
    if unreachable:
        return ProviderUnavailable("down")
    if status == 429 or error_type in BUSY_TYPES:
        return ProviderUnavailable("busy", retry_after=retry_after_seconds(headers))
    if (status is not None and status >= 500) or error_type in DOWN_TYPES:
        return ProviderUnavailable("down")
    return None


@contextlib.contextmanager
def translated(refusal: Callable[[Exception], ProviderUnavailable | None]) -> Iterator[None]:
    """Re-raise an SDK error that is a refusal as ``ProviderUnavailable``; anything else as-is."""
    try:
        yield
    except Exception as exc:
        unavailable = refusal(exc)
        if unavailable is None:
            raise
        raise unavailable from exc
