"""A deadline on every request, until it starts responding (S47).

A route that calls a model had no bound on how long it could run: the provider SDKs bound each
network read, and a lesson or curriculum makes several calls. This middleware cancels the
handler and answers 504 if no response has *started* by ``request_deadline_seconds``.

Once the response starts the limit is lifted, so a server-sent-event stream — whose headers go
out at once — is governed by its turn's own deadline (``app.services.turn_control``) instead.
It applies to every route, so no endpoint can be forgotten. A ``TimeoutError`` the handler
raises itself is its own error, not a deadline, and passes through.
"""

import asyncio
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings


class RequestDeadlineMiddleware:
    def __init__(self, app: ASGIApp, *, seconds: Callable[[], float] | None = None) -> None:
        self.app = app
        self.seconds = seconds or (lambda: get_settings().request_deadline_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        timeout = asyncio.timeout(self.seconds())

        async def send_and_lift(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                timeout.reschedule(None)
            await send(message)

        try:
            async with timeout:
                await self.app(scope, receive, send_and_lift)
        except TimeoutError:
            if not timeout.expired() or started:
                raise
            await JSONResponse({"detail": "deadline_exceeded"}, status_code=504)(
                scope, receive, send
            )
