"""`poe build-calibration-dataset` — mine the live DB into a gitignored calibration dataset.

Manual; needs a DB. Writes tests/eval/datasets/tracer-calibration.json (gitignored) and prints a
summary. Not part of `poe check` / `poe eval`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.db import SessionFactory
from tests.eval.datasets.mine import mine_observation_sequences

_OUT = Path(__file__).parent / "tracer-calibration.json"


async def _build() -> None:
    async with SessionFactory() as session:
        dataset = await mine_observation_sequences(session)
    dataset.to_file(_OUT)
    total_steps = sum(len(s.steps) for s in dataset.sequences)
    if dataset.sequences:
        print(f"wrote {_OUT}: {len(dataset.sequences)} sequences, {total_steps} steps")
    else:
        print(f"wrote {_OUT}: no data yet (0 sequences) — run some sessions first")


def main() -> int:
    asyncio.run(_build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
