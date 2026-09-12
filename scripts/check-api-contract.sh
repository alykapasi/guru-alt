#!/usr/bin/env bash
# Fail when the committed frontend types no longer match the backend's OpenAPI document (S58).
#
# The frontend and the backend agree on a contract that neither of them owns: `schema.d.ts` is
# generated from the API's OpenAPI document, and nothing checked that the committed copy still
# described the API. This regenerates it and fails on any difference, so an incompatible change
# blocks the PR instead of reaching a browser as a runtime error the type-checker approved.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
schema="$root/frontend/src/api/schema.d.ts"
spec="$(mktemp -t guru-openapi-XXXXXX.json)"
trap 'rm -f "$spec"' EXIT

echo "Dumping the API's OpenAPI document…"
# Redirect explicitly rather than through a pipe: a pipeline's exit status is the *last*
# command's, so a failure here would be reported as a success by whatever consumed it.
uv run python -m app.openapi_dump > "$spec"

echo "Regenerating ${schema}…"
( cd "$root/frontend" && npx --yes openapi-typescript@7.13.0 "$spec" -o src/api/schema.d.ts )

if ! git -C "$root" diff --quiet -- "$schema"; then
  echo
  echo "FAIL: frontend/src/api/schema.d.ts is out of date with the API." >&2
  echo "The backend's shape changed and the generated types were not regenerated." >&2
  echo "Run: uv run poe api-types    (then commit the result)" >&2
  echo >&2
  git -C "$root" --no-pager diff -- "$schema" >&2
  exit 1
fi

echo "OK: the frontend types match the API."
