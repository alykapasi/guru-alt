# The API and the worker are the same image with different commands: they share every
# dependency and every line of application code, and building them separately is how the two
# drift until a job runs against a schema the worker does not have.
FROM python:3.13-slim AS base

# Runtime libraries the ingestion path needs at import time, not just when it runs a job —
# a missing one fails the container at startup, which is when it should fail.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libmagic1 \
      poppler-utils \
      ffmpeg \
      curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Dependencies resolve from the lockfile alone, so a code change does not re-resolve them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY db ./db
COPY app ./app
RUN uv sync --frozen --no-dev

# Nothing here runs as root: an ingestion job executes parsers over files a learner uploaded.
RUN useradd --create-home --uid 10001 guru && chown -R guru:guru /app
USER guru

FROM base AS api
EXPOSE 8000
# Liveness only. Readiness is /api/v1/ready and belongs to the orchestrator, not the runtime:
# a dependency blip must not make Docker kill a healthy process.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
# No healthcheck: a taskiq worker exposes no port, and "the process is up" is precisely the
# thing that misleads here — watch /api/v1/ops/ingestion instead.
CMD ["taskiq", "worker", "app.workers.broker:broker", "app.workers.tasks"]
