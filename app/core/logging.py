"""Structured logging via structlog.

``configure_logging`` is called once at startup. Modules obtain a logger with
``structlog.get_logger(__name__)``. Request-scoped context (e.g. ``request_id``) is
bound via contextvars in :mod:`app.core.middleware` and merged into every event.
"""

import logging
import sys

import structlog

from app.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog processors and output format for the process."""
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
