"""Print the API's OpenAPI document — ``uv run poe openapi`` (S58).

The frontend's typed client is generated from this document, so the two are a contract with
two sides and no gate between them: `npm run gen:api` pointed at a *running* dev server, which
means the types were only ever as current as the last time somebody remembered to start one
and re-run it. A backend change that removed a field or renamed a route could be merged, and
the frontend would keep compiling against the shape that used to be there.

Dumping from the app object rather than over HTTP is what lets that become a check: it needs
no server, no database and no network, so CI can regenerate the types and fail when the
committed ones differ (``scripts/check-api-contract.sh``).
"""

import json
import sys


def main() -> None:
    # Imported here rather than at module scope: building the app runs settings validation and
    # the router imports, and doing that on import would make this module unimportable in any
    # environment that cannot construct the app at all.
    from app.main import app

    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
