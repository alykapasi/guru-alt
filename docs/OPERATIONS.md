# Operations runbook

What to run to deploy this, what to watch, and what to do when it breaks (tracker item S60).

**Related:** [RUNBOOK.md](RUNBOOK.md) is the *developer* runbook — local stack, evaluation
suite, debugging. This one is about deploying the thing and getting it back when it breaks.

Every command here has been executed against the local stack. Where something has *not* been
rehearsed against real infrastructure, it says so — an untested runbook is worse than no
runbook, because it is read for the first time during an incident.

## What runs

| Process | Image target | Command |
| --- | --- | --- |
| API | `api` | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| Worker | `worker` | `taskiq worker app.workers.broker:broker app.workers.tasks` |
| Migrations | `api` | `alembic upgrade head` |

One image, two commands. Building them separately is how the worker ends up running a job
against a schema it does not have.

Dependencies: PostgreSQL 17 with pgvector, Redis, and an S3-compatible object store.

```bash
# The whole thing, locally, exactly as it deploys:
docker compose -f docker-compose.yml -f docker-compose.app.yml up -d --build
curl -fsS localhost:8000/health          # liveness
curl -fsS localhost:8000/api/v1/ready    # readiness, per dependency
```

## Configuration

Every setting is `GURU_<FIELD>`; the full list is `app/core/config.py`, and `.env.example` is
the annotated version. Starting with `GURU_ENV=prod` **refuses to boot** while any development
default is still in place — the localhost database, the MinIO credentials, a wildcard CORS
origin, debug output, no model provider key, the development sign-in seam, or a session cookie
that would cross plain HTTP (`app/core/release.py`). The process reports all of them at once,
so a bad deploy costs one restart rather than one per discovered problem.

Secrets that must be set explicitly in production:

| Variable | Why |
| --- | --- |
| `GURU_DATABASE_URL` | Contains the database password |
| `GURU_BLOB_ACCESS_KEY` / `GURU_BLOB_SECRET_KEY` | Object store credentials |
| `GURU_ANTHROPIC_API_KEY` and/or `GURU_OPENROUTER_API_KEY` | Model access; at least one |
| `GURU_CORS_ORIGINS` | The real frontend origin, not `*` |
| `GURU_SESSION_COOKIE_SECURE=true` | Without it the session cookie is sent over plain HTTP, where anything on the path can read and replay it |
| `GURU_DEV_AUTO_LOGIN=false` | `POST /auth/dev-login` issues a session with **no credential** — it is not a weak password, it is no password |

### Identity (S21)

Learners sign in with an email address and a password (Argon2id), and hold an opaque session
token in an httpOnly cookie; the same token is accepted as `Authorization: Bearer` for
non-browser clients. Sessions are rows in `learner_sessions`, checked on every request, so
revocation is immediate — `POST /auth/logout-all` ends every session a learner has, and
deleting an account cascades theirs away.

The worker prunes sessions that can no longer authenticate anybody every
`GURU_SESSION_PURGE_INTERVAL_SECONDS` (default hourly; `0` turns it off). A revoked row is kept
for `GURU_SESSION_REVOKED_RETENTION_HOURS` first, so "your session was ended" stays
distinguishable from a token that never existed.

Set `GURU_SESSION_COOKIE_SAMESITE=none` **only** alongside `GURU_SESSION_COOKIE_SECURE=true`,
and only when the app and API are genuinely cross-site; `lax` is correct when they share a
registrable domain, and it is the browser's own CSRF protection.

## Health, readiness, and what to alert on

`/health` is **liveness**: the process is up. It touches nothing, deliberately — a supervisor
restarts on failure here, and a database blip must not kill every healthy instance at once.

`/api/v1/ready` is **readiness**: each dependency probed concurrently with a 2s budget,
`503` when any fails. Route traffic on this one.

`/api/v1/ops/ingestion` is the queue. **Alert on `oldest_pending_age_seconds`** — it rises the
moment the queue stops draining and keeps rising, where `pending` alone can hold steady while
nothing is processed at all.

| Signal | Means | Do |
| --- | --- | --- |
| `stalled` is true | Work waiting, nothing in flight | The worker is dead or not consuming. Check worker logs, restart it. |
| `oldest_pending_age_seconds` climbing, `processing` at `max_concurrent_jobs` | Saturated, not stuck | Add worker replicas. |
| `expired_leases` > 0 for longer than `GURU_INGEST_RECONCILE_INTERVAL_SECONDS` | The reconciler is not running | `uv run poe reconcile-ingestion` to sweep now, then find out why the worker's timer is not firing. |
| `failed` rising | Sources exhausting `ingest_max_attempts` | Read `sources.error`; these are parked, not retried. |

Cost and token use are logged per call (`llm_calls`, tagged by role and model) — see
`app/services/accounting.py`. There is no alerting on spend yet; see *Not covered*.

## Deploying a release

Migrations are expand-then-contract, which is what makes a rollback possible: a release must
never ship a migration that the *previous* version of the code cannot run against.

1. `alembic upgrade head` — as its own step, to completion, before any new instance serves. The
   compose overlay enforces this with `service_completed_successfully`.
2. Roll the API, then the worker.
3. Watch `/api/v1/ready` on the new instances and `oldest_pending_age_seconds` for the queue.

### Rolling back

Roll the **code** back first and leave the schema alone. An expand-only migration is safe for
the previous version by construction, so this is the whole procedure in the normal case.

Only if a migration must also come out:

```bash
uv run poe db-downgrade          # one revision
```

Every migration in this repo is round-tripped in CI (`upgrade head → downgrade -1 → upgrade
head`), so `downgrade` is known to run. It is not known to be *lossless*: a contracting
migration drops columns, and downgrading past one loses what was in them. Take a backup first.

## Backup and restore

```bash
# Back up (the compose Postgres; adapt the container for a managed instance):
docker compose exec -T postgres pg_dump -U guru -Fc guru > guru-$(date +%F).dump

# Restore into an empty database:
docker compose exec -T postgres dropdb   -U guru --if-exists guru_restore
docker compose exec -T postgres createdb -U guru guru_restore
docker compose exec -T postgres pg_restore -U guru -d guru_restore --no-owner < guru-YYYY-MM-DD.dump
```

`uv run poe backup-drill` runs exactly that cycle — dump, restore into a scratch database,
compare row counts across every table, drop the scratch database — and exits non-zero if
anything differs. Run it on a schedule. A backup nobody has restored is a hypothesis.

**The object store is not in the database dump.** Blobs live under `blobs/<sha256>` and are
shared across learners by content hash (S77); `sources.blob_key` references them. A restored
database with an empty bucket has sources that cannot be re-ingested. Back the bucket up
separately (`mc mirror`, or the provider's own replication).

## Not covered

Honest gaps, so nobody discovers them mid-incident:

- **No rehearsal against real infrastructure.** The build, the migration step, both processes,
  the readiness behaviour under a stopped dependency, the full migration round-trip and the
  backup drill have all been executed — against the local compose stack. The managed-Postgres
  and real-S3 equivalents are untested, and so is anything about network policy, TLS
  termination or secret delivery.
- **No alerting.** The signals above exist and are worth polling; nothing polls them, and there
  is no paging, dashboard, or error tracker wired up.
- **No spend alerting.** Cost is recorded per call and bounded per learner
  (`learner_daily_token_limit`); nothing watches the total.
- **No blue/green or canary.** The procedure above is a rolling restart.
- **Restore is not automated.** `backup-drill` proves a dump restores; promoting a restored
  database to primary is a manual decision and a manual DSN change.
- **No migration test against representative existing data** — only against a fresh database
  (tracker item S58).
