"""Bring stale sources up to date — ``uv run poe reindex [--apply] [--reextract]
[--learner ID] [--limit N]``. Dry run by default. See docs/RUNBOOK.md §15."""

import argparse
import asyncio
import uuid

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.llm.embedding_space import current_space
from app.llm.registry import build_llm_client
from app.services import reindex
from app.workers.tasks import _enqueue_ingestion


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    llm = build_llm_client(settings)
    space = current_space(llm, dim=settings.embed_dim)
    async with SessionFactory() as session:
        found = await reindex.plan(session, space=space, learner_id=args.learner)
        result = None
        if args.apply:
            result = await reindex.apply(
                session,
                llm,
                found,
                space=space,
                reextract=args.reextract,
                limit=args.limit,
                enqueue=_enqueue_ingestion,
                settings=settings,
            )
        print(reindex.render(found, result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="re-embed and repair scope")
    parser.add_argument(
        "--reextract", action="store_true", help="with --apply: also re-ingest stale extractions"
    )
    parser.add_argument("--learner", type=uuid.UUID, help="only this learner's sources")
    parser.add_argument("--limit", type=int, help="at most N sources re-embedded/re-extracted")
    args = parser.parse_args()
    if args.reextract and not args.apply:
        parser.error("--reextract needs --apply")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
