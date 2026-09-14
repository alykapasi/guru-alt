#!/usr/bin/env bash
# The API the browser journeys drive (S58): its own database, and no model behind it.
#
# Its own database because the journey *commits* — it signs in, writes messages, uploads
# sources — so it cannot use the suite's transactional fixtures, and pointing it at either the
# developer's database or the suite's would let a browser run and a test run corrupt each
# other's assertions. Derived from GURU_DATABASE_URL the same way the suite's is, so there is
# no third DSN to keep in sync.
#
# No model behind it because a journey that calls a real provider is neither offline nor
# repeatable, and would bill for every CI run. Every role routes to the deterministic fake
# provider; `app/core/release.py` refuses to start production that way.
set -euo pipefail

cd "$(dirname "$0")/.."

export GURU_ENV=dev
GURU_DATABASE_URL="$(uv run python -m tests.testdb --suffix _e2e --print-url)"
export GURU_DATABASE_URL
export GURU_MODEL_FAST=fake:fake-1
export GURU_MODEL_SMART=fake:fake-1
export GURU_MODEL_GENIUS=fake:fake-1
export GURU_MODEL_VISION=fake:fake-1
export GURU_MODEL_EMBED=fake:fake-1
# Deliberately no GURU_DEV_AUTO_LOGIN. The journeys register through the form, which is both
# the path a first user takes and the only one available: the development sign-in button is
# compiled out of the production bundle these run against. Leaving the seam off also lets the
# first journey assert that a signed-out browser is turned away.

exec uv run uvicorn app.main:app --host 127.0.0.1 --port "${GURU_E2E_API_PORT:-8000}" --log-level warning
