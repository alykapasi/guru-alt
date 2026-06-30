"""FastAPI application entry point."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import configure_logging
from app.core.middleware import request_id_middleware

settings = get_settings()
configure_logging(settings)
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown: log lifecycle and dispose the DB engine on exit."""
    log.info("app.startup", env=str(settings.env))
    yield
    await engine.dispose()
    log.info("app.shutdown")


app = FastAPI(title="Guru API", version="0.1.0", lifespan=lifespan)
app.middleware("http")(request_id_middleware)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}
