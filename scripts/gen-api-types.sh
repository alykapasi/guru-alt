#!/usr/bin/env bash
# Regenerate the frontend's typed client from the backend's OpenAPI document (S58).
# No running server needed — the document comes from the app object.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
spec="$(mktemp -t guru-openapi-XXXXXX.json)"
trap 'rm -f "$spec"' EXIT

uv run python -m app.openapi_dump > "$spec"
( cd "$root/frontend" && npx --yes openapi-typescript@7.13.0 "$spec" -o src/api/schema.d.ts )
echo "Wrote frontend/src/api/schema.d.ts"
