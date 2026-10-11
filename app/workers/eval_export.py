"""Write an area's labelled examples as JSONL — ``uv run poe eval-export <area>`` (S59).

Each line keeps the example's id and the reference to the row it was copied from, so a private
evaluation copy stays traceable (V14). The default destination is git-ignored: these are a
learner's own words and must never be committed.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory
from app.models.evaluation import AREAS, EvalExample

PRIVATE_DIR = Path(__file__).resolve().parents[2] / "tests" / "eval" / "datasets" / "private"


def default_path(area: str) -> Path:
    return PRIVATE_DIR / f"{area}.jsonl"


async def export(session: AsyncSession, area: str, path: Path) -> int:
    rows = (
        await session.scalars(
            select(EvalExample)
            .where(EvalExample.area == area, EvalExample.label.isnot(None))
            .order_by(EvalExample.labelled_at)
        )
    ).all()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in rows:
            fh.write(
                json.dumps(
                    {
                        "id": str(e.id),
                        "area": e.area,
                        "source_kind": e.source_kind,
                        "source_id": str(e.source_id),
                        "snapshot": e.snapshot,
                        "decision": e.decision,
                        "label": e.label,
                        "note": e.note,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)


async def _main(area: str, path: Path) -> None:
    async with SessionFactory() as session:
        count = await export(session, area, path)
    print(f"wrote {count} labelled {area} example(s) to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export labelled evaluation examples.")
    parser.add_argument("area", choices=AREAS)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(_main(args.area, args.out or default_path(args.area)))


if __name__ == "__main__":
    main()
