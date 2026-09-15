"""Make a learner an administrator — ``uv run poe grant-admin <email>`` (P10).

The bootstrap has to live outside the API, because the API's own answer to "who may grant
admin" is "an administrator", and a deployment starts with none. So the first one is granted
by somebody holding the database, which is the only authority that exists before there is an
administrator to delegate to. It sits beside the other operator commands rather than in a
package of its own.

Granting and revoking are the same command, because an administrator who cannot be demoted is
a worse problem than one who cannot be appointed. ``--revoke`` takes it away.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.db import SessionFactory
from app.models.learner import Learner
from app.services.auth import normalise_email


async def run(email: str, *, revoke: bool) -> int:
    async with SessionFactory() as session:
        learner = await session.scalar(
            select(Learner).where(Learner.email == normalise_email(email))
        )
        if learner is None:
            print(f"no learner with email {email!r}", file=sys.stderr)
            return 1
        if learner.is_admin == (not revoke):
            print(f"{email} is already {'not ' if revoke else ''}an administrator.")
            return 0
        learner.is_admin = not revoke
        await session.commit()

    print(f"{email} is {'no longer ' if revoke else 'now '}an administrator.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Make a learner an administrator.")
    parser.add_argument("email", help="the learner's sign-in address")
    parser.add_argument("--revoke", action="store_true", help="take the grant away instead")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.email, revoke=args.revoke)))


if __name__ == "__main__":
    main()
