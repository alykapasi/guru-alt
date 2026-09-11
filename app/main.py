"""FastAPI application entry point."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_llm_client
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import configure_logging
from app.core.middleware import request_id_middleware
from app.core.release import enforce_production_settings

settings = get_settings()
configure_logging(settings)
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup/shutdown: validate config and the model registry, log lifecycle, dispose engine."""
    # Development defaults that are wrong in production fail here rather than at the first
    # request that depends on them — a misconfigured instance must not reach a readiness
    # probe and start taking traffic (S60).
    enforce_production_settings(settings)
    # Building the registry validates the role→provider map (see llm.registry). Doing it here
    # turns a typo in GURU_MODEL_* into a refusal to start, not a 500 mid-conversation.
    get_llm_client()
    log.info("app.startup", env=str(settings.env))
    yield
    await engine.dispose()
    log.info("app.shutdown")


app = FastAPI(title="Guru API", version="0.1.0", lifespan=lifespan)
app.middleware("http")(request_id_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Liveness only — deliberately touches nothing.

    A supervisor restarts the process when this fails, so it must answer for the process and
    nothing else. Readiness, which does talk to dependencies, is `/api/v1/ready`: conflating
    the two means a database blip gets every healthy instance killed at once.
    """
    return {"status": "ok"}
