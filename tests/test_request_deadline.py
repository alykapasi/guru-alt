"""Every request is bounded until it starts responding (S47)."""

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.core.deadline import RequestDeadlineMiddleware
from app.main import app as guru_app

cancelled: list[str] = []


async def slow(request):
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        cancelled.append("slow")
        raise
    return JSONResponse({"ok": True})


async def fast(request):
    return JSONResponse({"ok": True})


async def own_timeout(request):
    raise TimeoutError("the handler's own")


async def stream(request):
    async def body():
        yield b"data: a\n\n"
        await asyncio.sleep(0.3)
        yield b"data: b\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _client(seconds: float) -> httpx.AsyncClient:
    inner = Starlette(
        routes=[
            Route("/slow", slow),
            Route("/fast", fast),
            Route("/own", own_timeout),
            Route("/stream", stream),
        ]
    )
    wrapped = RequestDeadlineMiddleware(inner, seconds=lambda: seconds)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped, raise_app_exceptions=True),
        base_url="http://t",
    )


async def test_a_slow_request_answers_504_and_its_handler_is_cancelled() -> None:
    cancelled.clear()
    async with _client(0.05) as c:
        r = await c.get("/slow")
    assert (r.status_code, r.json()) == (504, {"detail": "deadline_exceeded"})
    assert cancelled == ["slow"]


async def test_a_fast_request_is_untouched() -> None:
    async with _client(1) as c:
        assert (await c.get("/fast")).json() == {"ok": True}


async def test_a_stream_runs_past_the_limit_once_it_has_started() -> None:
    async with _client(0.1) as c:
        r = await c.get("/stream")
    assert r.status_code == 200
    assert "data: b" in r.text


async def test_a_handlers_own_timeout_is_not_a_504() -> None:
    async with _client(1) as c:
        try:
            r = await c.get("/own")
        except TimeoutError as exc:
            assert "the handler's own" in str(exc)
        else:
            assert r.status_code != 504


def test_the_app_installs_it() -> None:
    assert any(m.cls is RequestDeadlineMiddleware for m in guru_app.user_middleware)
