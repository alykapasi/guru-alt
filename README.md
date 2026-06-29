# Guru Alt

API-first platform for web, mobile, and desktop applications.

## Tech Stack

- **Backend:** FastAPI, Uvicorn, Pydantic, asyncio
- **Database:** PostgreSQL with pgvector, GIN extensions
- **Frontend:** React, TypeScript, Vite (planned)
- **Tools:** uv, ruff, beartype, pytest, poethepoet

## Getting Started

### Prerequisites

- Python 3.13+
- uv (package manager)

### Installation

```bash
uv sync
```

### Development

Start the dev server:

```bash
uv run poe dev
```

The API will be available at `http://localhost:8000`. OpenAPI docs at `http://localhost:8000/docs`.

## Commands

All commands are orchestrated through `poethepoet`. Run `uv run poe --help` to see all available tasks.

### Common Tasks

```bash
# Dev server
uv run poe dev

# Testing
uv run poe test           # Run all tests
uv run poe test-watch    # Run tests in watch mode

# Code quality
uv run poe lint          # Check code with ruff
uv run poe format        # Format code with ruff
uv run poe format-check  # Check formatting without changing

# Database
uv run poe db-upgrade    # Run migrations
uv run poe db-downgrade  # Rollback one migration
```

## Project Structure

```text
guru-alt/
├── app/              # FastAPI application
│   ├── main.py      # App entry point and routes
│   └── __init__.py
├── db/               # Database models and setup
│   └── __init__.py
├── tests/            # Test suite
│   ├── test_*.py    # Test files
│   └── __init__.py
├── pyproject.toml    # Project config and dependencies
├── CLAUDE.md         # Guidance for Claude Code
└── README.md         # This file
```

## Development Philosophy

- Pragmatic Programmer principles: DRY, YAGNI, focused on conciseness and performance
- Plan before implementing; iterate with small, verified changes
- Type safety with beartype runtime checking and Pydantic validation
- Async-first design for efficiency
