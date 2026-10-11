"""Mark an account eligible for evaluation sampling — ``uv run poe eval-eligible <email>`` (S59).

Only an eligible account's activity is ever copied into evaluation examples, or has its
practice intent recorded for sampling. Off by default and set from outside the API, like
``grant-admin``: deciding whose data an evaluation may read is an operator's act, not a click.
``--revoke`` stops new sampling; examples already drawn are kept.
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
        learner.eval_eligible = not revoke
        await session.commit()

    if revoke:
        print(f"{email} is no longer eligible for evaluation sampling; drawn examples are kept.")
    else:
        print(f"{email} is now eligible for evaluation sampling.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Mark an account eligible for evaluation.")
    parser.add_argument("email", help="the learner's sign-in address")
    parser.add_argument("--revoke", action="store_true", help="stop sampling this account")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.email, revoke=args.revoke)))


if __name__ == "__main__":
    main()
