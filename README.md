# Guru

**An AI-first personalized learning platform** whose promise is *durable* learning — knowledge that
sticks. Guru adapts content, pacing, scaffolding, and assessment to each learner, with a hierarchical
knowledge graph, a continuous mastery model, and an evidence-based learner profile at its core.

> **Design docs (read these to understand the project):**
> [docs/MASTERPLAN.md](docs/MASTERPLAN.md) (vision + architecture) ·
> [docs/ROADMAP.md](docs/ROADMAP.md) (phased plan) ·
> [docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md) (engineering detail).

Note: "alt" is a version suffix for the current build, not part of the product name.

---

## Tech Stack

- **Backend:** Python 3.13 · FastAPI · Uvicorn · Pydantic · async SQLAlchemy 2.0
- **Database:** PostgreSQL 17 + pgvector (HNSW) + pg_trgm/GIN, via Alembic migrations
- **Tooling:** [uv](https://docs.astral.sh/uv/) (packaging) · ruff (lint/format) · ty (types) ·
  beartype (runtime types) · pytest · poethepoet (task runner) · pre-commit
- **Frontend:** React + TypeScript + Vite (later phase)

---

## Prerequisites

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Docker + Docker Compose** (for Postgres and Redis)

---

## Quickstart

```bash
# 1. Install dependencies (creates the .venv automatically)
uv sync

# 2. Create your local env file
cp .env.example .env

# 3. Start Postgres (pgvector) + Redis
docker compose up -d

# 4. Apply database migrations (enables vector + pg_trgm, etc.)
uv run poe db-upgrade

# 5. Run the dev server
uv run poe dev

# 6. (Recommended) install git pre-commit hooks
uv run poe hooks-install
```

The API is now at <http://localhost:8000> — health check at
[`/health`](http://localhost:8000/health), interactive OpenAPI docs at
[`/docs`](http://localhost:8000/docs).

> **Note on ports:** Postgres is published on host port **5433** (not the default 5432) to avoid
> clashing with a local Postgres. This matches `GURU_DATABASE_URL` in `.env.example`.

---

## Commands

All tasks run through `poethepoet`. List them with `uv run poe --help`.

| Command | Description |
| ------- | ----------- |
| `uv run poe dev` | Start the FastAPI dev server (auto-reload) |
| `uv run poe test` | Run the test suite |
| `uv run poe test-watch` | Run tests fail-fast in watch mode |
| `uv run poe lint` | Lint with ruff |
| `uv run poe format` | Auto-format with ruff |
| `uv run poe format-check` | Check formatting without modifying |
| `uv run poe type-check` | Type-check with ty |
| `uv run poe check` | **The green gate:** lint + type-check + test |
| `uv run poe db-upgrade` | Apply migrations (`alembic upgrade head`) |
| `uv run poe db-downgrade` | Roll back one migration |
| `uv run poe hooks-install` | Install the git pre-commit hooks |
| `uv run poe hooks` | Run all pre-commit hooks against the whole repo |

---

## Configuration

All settings are environment variables prefixed `GURU_` (see [`.env.example`](.env.example)), loaded
via pydantic-settings. Key ones:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `GURU_ENV` | `dev` | `dev` \| `test` \| `prod` (controls log style + runtime type-checking) |
| `GURU_DATABASE_URL` | `postgresql+asyncpg://guru:guru@localhost:5433/guru` | Async Postgres URL |
| `GURU_REDIS_URL` | `redis://localhost:6379/0` | Redis (used from Phase 4) |
| `GURU_LOG_LEVEL` | `INFO` | Log level |
| `GURU_LOG_JSON` | `false` | `true` for JSON logs (prod); console otherwise |

`.env` is git-ignored. Outside `prod`, beartype runtime type-checking is active across the `app`
package.

---

## Testing

```bash
uv run poe test          # all tests
uv run poe check         # lint + type-check + test (what CI runs)
```

Unit tests run offline and deterministically. Integration tests (Phase 1+) use the Postgres started
by `docker compose`.

---

## Quality & CI

- **Pre-commit hooks** ([`.pre-commit-config.yaml`](.pre-commit-config.yaml)) run ruff (lint+format)
  and ty on staged files, plus basic hygiene checks. Install with `uv run poe hooks-install`; run
  everything with `uv run poe hooks`. They use the project's pinned tool versions via `uv run`.
- **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs format-check, lint,
  type-check, tests, and a migration-apply check against a pgvector service on every push to `main`
  and every PR.

---

## Project Structure

```text
guru-alt/
├── app/
│   ├── __init__.py        # enables beartype runtime checks (dev/test)
│   ├── main.py            # FastAPI app: lifespan, middleware, /health
│   └── core/              # config, db (engine/session/Base), logging, middleware
├── db/migrations/         # Alembic (async); 0001 enables vector + pg_trgm
├── tests/                 # pytest suite
├── docs/                  # MASTERPLAN · ROADMAP · TECHNICAL_DESIGN
├── docker-compose.yml     # Postgres (pgvector) + Redis
├── pyproject.toml         # deps + poe tasks + tool config
├── .pre-commit-config.yaml
└── CLAUDE.md              # guidance for Claude Code
```

The backend layering and the learning engine are documented in
[docs/TECHNICAL_DESIGN.md](docs/TECHNICAL_DESIGN.md).

---

## Development Philosophy

Pragmatic Programmer principles (DRY, YAGNI), conciseness and performance, plan-before-build with
small verified increments, async-first, and type safety via Pydantic + beartype + ty. Every change
should leave `uv run poe check` green.
