"""Verify the object store still holds what the database references — ``uv run poe blob-check``.

Run this **after a restore** and before letting learners back in: the database dump does not
carry the uploaded bytes, so a restored database with an empty bucket has a library that looks
intact and sources that cannot be re-ingested (S60, S77). Worth running on a schedule too, to
catch a bucket lifecycle rule expiring objects the database still points at.

Exits non-zero when anything is missing, so it can be a deployment gate rather than something
somebody reads.
"""

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.services import blob_integrity
from app.storage import build_blob_store


async def run() -> int:
    store = build_blob_store(get_settings())
    async with SessionFactory() as session:
        report = await blob_integrity.check(session, store)

    print(f"checked {report.checked} referenced blob(s)")
    if report.intact:
        print("OK: every blob the database references is present.")
        return 0

    print(f"MISSING {len(report.missing)}:", file=sys.stderr)
    for entry in report.missing:
        print(
            f"  {entry.blob_key}  source={entry.source_id}  origin={entry.origin}", file=sys.stderr
        )
    print(
        "\nThese sources cannot be re-ingested: the bytes are gone and content-addressed "
        "de-duplication means nothing else holds a copy. Restore the bucket from its own "
        "backup (see docs/OPERATIONS.md).",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
