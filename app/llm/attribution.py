"""Who a model call is for, and what it is for (S48).

Every call used to be recorded by hand at the call site, 29 of them, each remembering to pass
the learner and forgetting everything else — which feature paid, which request. The client now
records every call itself and reads the answer from here: entry points set the learner and the
request, the service doing the work sets the feature, and a nested ``attributed`` overrides only
what it names.

Restored by saving and re-setting the previous value rather than with a ``ContextVar`` token:
an async generator can be resumed from a context other than the one that set its token, and a
token reset there raises. One consequence is accepted: while a metered generator is suspended
at a ``yield``, its caller sees the generator's attribution. The caller is the route streaming
the events, which is the same work.
"""

import functools
import inspect
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Attribution:
    learner_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    feature: str | None = None
    request_id: str | None = None
    background: bool = False


_NOTHING = Attribution()  # frozen, so safe to share as the default
_current: ContextVar[Attribution] = ContextVar("llm_attribution", default=_NOTHING)


def current() -> Attribution:
    return _current.get()


@contextmanager
def attributed(**fields: Any) -> Iterator[Attribution]:
    """Override the named fields for the duration of the block."""
    previous = _current.get()
    value = replace(previous, **fields)
    _current.set(value)
    try:
        yield value
    finally:
        _current.set(previous)


def bind(**fields: Any) -> None:
    """Set fields for the rest of the current task, with no restore.

    For a request dependency: it returns before the endpoint runs, so a ``with`` block would
    already have ended. Each request runs in its own task with its own copy of the context,
    so nothing set here outlives the request.
    """
    _current.set(replace(_current.get(), **fields))


def _pick(bound: dict[str, Any], path: str | None) -> Any:
    if path is None:
        return None
    name, _, attribute = path.partition(".")
    value = bound.get(name)
    return getattr(value, attribute) if attribute and value is not None else value


def metered(
    feature: str,
    *,
    learner: str | None = None,
    conversation: str | None = None,
    background: bool | None = None,
) -> Callable:
    """Attribute every model call made inside the decorated function.

    ``learner`` / ``conversation`` name an argument, optionally with one attribute
    (``"conversation.id"``, ``"source.learner_id"``); omitted, they are inherited.
    """

    def decorate(fn: Callable) -> Callable:
        signature = inspect.signature(fn)

        def fields(args: tuple, kwargs: dict) -> dict[str, Any]:
            bound = signature.bind_partial(*args, **kwargs).arguments
            out: dict[str, Any] = {"feature": feature}
            if learner is not None:
                out["learner_id"] = _pick(bound, learner)
            if conversation is not None:
                out["conversation_id"] = _pick(bound, conversation)
            if background is not None:
                out["background"] = background
            return out

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def generator(*args: Any, **kwargs: Any):
                with attributed(**fields(args, kwargs)):
                    async for item in fn(*args, **kwargs):
                        yield item

            return generator

        @functools.wraps(fn)
        async def coroutine(*args: Any, **kwargs: Any) -> Any:
            with attributed(**fields(args, kwargs)):
                return await fn(*args, **kwargs)

        return coroutine

    return decorate
