#!/usr/bin/env bash
# `poe backup-drill` — dump the database, restore it into a scratch copy, and prove the copy
# holds the same rows (S60).
#
# A backup nobody has restored is a hypothesis. This turns it into a check that can run on a
# schedule and fail loudly, rather than a procedure discovered to be wrong during an incident.
#
# Runs against the docker-compose Postgres by default. Point it at another instance with
# PG_CONTAINER, or adapt the three exec lines for a managed one.
set -euo pipefail

PG_CONTAINER="${PG_CONTAINER:-postgres}"
DB="${PGDATABASE:-guru}"
USER="${PGUSER:-guru}"
SCRATCH="${DB}_restore_drill"
DUMP="$(mktemp -t guru-drill-XXXXXX.dump)"
trap 'rm -f "$DUMP"' EXIT

pg() { docker compose exec -T "$PG_CONTAINER" "$@"; }

# Per-table row counts, as "table count" lines. query_to_xml runs the count for each table in
# one statement, so the two sides are each a single consistent read.
counts() {
  pg psql -U "$USER" -d "$1" -At -F' ' -c "
    SELECT relname, (xpath('/row/c/text()', x))[1]::text::bigint
    FROM (
      SELECT relname,
             query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname),
                          false, true, '') AS x
      FROM pg_stat_user_tables WHERE schemaname = 'public'
    ) t ORDER BY relname;"
}

echo "dumping $DB ..."
pg pg_dump -U "$USER" -Fc "$DB" > "$DUMP"
echo "dump: $(wc -c < "$DUMP" | tr -d ' ') bytes"

echo "restoring into $SCRATCH ..."
pg dropdb   -U "$USER" --if-exists "$SCRATCH"
pg createdb -U "$USER" "$SCRATCH"
# --no-owner so the drill works when the dump was taken as a different role. Errors are fatal:
# a restore that "mostly worked" is the failure mode this exists to catch.
pg pg_restore -U "$USER" -d "$SCRATCH" --no-owner --exit-on-error < "$DUMP"

before="$(counts "$DB")"
after="$(counts "$SCRATCH")"
pg dropdb -U "$USER" --if-exists "$SCRATCH"

if [[ "$before" != "$after" ]]; then
  echo "MISMATCH between $DB and its restored copy:" >&2
  diff <(echo "$before") <(echo "$after") >&2 || true
  exit 1
fi
echo "OK — $(wc -l <<< "$before" | tr -d ' ') tables, row counts identical"

# The dump does not contain the uploaded bytes. Blobs live in the object store and are shared
# by content hash (S77), so a database that restores perfectly alongside an empty bucket gives
# you a library that looks intact and sources that cannot be re-ingested from anything. A
# restore drill that stops at the row counts would report exactly that as a success.
echo
echo "checking the object store still holds what the database references ..."
uv run poe blob-check
