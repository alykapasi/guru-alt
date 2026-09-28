"""Put past answers through a grader again — ``uv run poe regrade [--run] [--since DAYS]
[--learner ID] [--subject ID] [--limit N] [--prompt current|recorded] [--model P:M]
[--out PATH]``. Dry run by default; never changes a grade. See docs/RUNBOOK.md §19."""

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.learning.rubric_grading import GRADING_ROLE
from app.llm.registry import ModelSpec, build_llm_client
from app.services import (
    regrade,
    spend_guard,  # noqa: F401  installs the spend guard on the meter (S47)
)


async def run(args: argparse.Namespace) -> int:
    llm = build_llm_client(get_settings())
    if args.model:
        provider, _, model = args.model.partition(":")
        llm = llm.with_roles({GRADING_ROLE: ModelSpec(provider, model)})
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionFactory() as session:
        found = await regrade.plan(
            session,
            since=now - timedelta(days=args.since),
            until=now,
            learner_id=args.learner,
            subject_id=args.subject,
            limit=args.limit,
        )
    cost = regrade.estimate_cost(found, llm)
    report = await regrade.run(llm, found, prompt=args.prompt) if args.run else None
    print(regrade.render(found, report, cost))
    if report is not None:
        with open(args.out, "w") as handle:
            json.dump(regrade.as_json(report), handle, indent=2)
        print(f"report written to {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="make the grading calls (paid)")
    parser.add_argument("--since", type=int, default=7, help="days back (default 7)")
    parser.add_argument("--learner", type=uuid.UUID)
    parser.add_argument("--subject", type=uuid.UUID)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--prompt", choices=("current", "recorded"), default="current")
    parser.add_argument("--model", help="provider:model for the grader (default: current)")
    parser.add_argument("--out", default="regrade-report.json")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
