"""HTTP middleware.

``request_id_middleware`` assigns a request id (honoring an inbound ``X-Request-ID``),
binds it into structlog's contextvars so every log line in the request carries it, and
echoes it back on the response.
"""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"


async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Bind a request id to the logging context for the duration of the request."""
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers[REQUEST_ID_HEADER] = request_id
    return response
