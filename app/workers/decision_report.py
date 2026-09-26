"""Print the decision report — ``uv run poe decision-report [--since YYYY-MM-DD] [--examples N]``.

Reads the `decision_calls` rows written while a Jev question was in shadow or live mode, and
prints what switching each question live would have meant. See docs/RUNBOOK.md §14.
"""

import argparse
import asyncio
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.services.decision_report import build_report


async def run(since: datetime | None, examples: int) -> int:
    settings = get_settings()
    async with SessionFactory() as session:
        print(
            await build_report(
                session,
                since=since,
                intent_threshold=settings.decision_intent_threshold,
                grade_threshold=settings.decision_fully_correct_threshold,
                examples=examples,
            )
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="only rows on or after this date (YYYY-MM-DD)")
    parser.add_argument("--examples", type=int, default=10, help="examples per list (0: none)")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None
    return asyncio.run(run(since, args.examples))


if __name__ == "__main__":
    raise SystemExit(main())
