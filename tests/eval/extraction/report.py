"""``uv run poe extraction-report`` — what the corpus says about its own extraction (S27).

Every chunk used to be stored with ``"confidence": 1.0``, computed by nothing and read by
nothing. It is now stored with measured indicators of damage
(:mod:`app.rag.extraction_quality`), and this is where they become readable: the distribution
across the learner's whole library, and the chunks that trip the most.

**There is no pass mark here, deliberately**, for the same reason the reliability report has
none. Whether an isolated-letter ratio of 0.2 is a ruined scan or a page of diagram captions
is not something anybody has measured, and picking a threshold before looking at a real
distribution is exactly the guess S18 exists to count. This prints what is true and leaves the
line to a person.

Read the zero case carefully: **no indicator is the absence of evidence, not evidence of
absence.** A PDF whose two columns were interleaved line by line produces flawless words in
meaningless order and trips nothing below.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import select

from app.core.db import SessionFactory
from app.models.source import Chunk
from app.rag.extraction_quality import ExtractionIndicators

RATIOS = ("isolated_letter_ratio", "vowelless_word_ratio", "runaway_token_ratio")
COUNTS = ("replacement_chars", "control_chars")


@dataclass
class Row:
    chunk_id: str
    method: str
    indicators: ExtractionIndicators


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, so every number printed is a value some chunk actually had rather than an
    interpolation between two of them."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def render(rows: list[Row]) -> str:
    if not rows:
        return (
            "Extraction quality\n"
            "  no chunks carry indicators — either nothing is ingested, or everything predates\n"
            "  the measurement and still carries the old asserted confidence.\n"
        )

    by_method: dict[str, int] = {}
    for row in rows:
        by_method[row.method] = by_method.get(row.method, 0) + 1

    lines = [
        "Extraction quality",
        f"  chunks            {len(rows)}",
        "  by method         " + ", ".join(f"{m}={n}" for m, n in sorted(by_method.items())),
        "",
        "  indicator                 median      p90      max   chunks>0",
    ]
    for name in RATIOS:
        values = [getattr(r.indicators, name) for r in rows]
        hit = sum(1 for v in values if v > 0)
        lines.append(
            f"  {name:<24} {_percentile(values, 0.5):.4f}   {_percentile(values, 0.9):.4f}   "
            f"{max(values):.4f}   {hit:>6}"
        )
    for name in COUNTS:
        values = [float(getattr(r.indicators, name)) for r in rows]
        hit = sum(1 for v in values if v > 0)
        lines.append(
            f"  {name:<24} {_percentile(values, 0.5):>6.0f}   {_percentile(values, 0.9):>6.0f}   "
            f"{max(values):>6.0f}   {hit:>6}"
        )

    worst = sorted(
        rows,
        key=lambda r: (
            r.indicators.replacement_chars
            + r.indicators.control_chars
            + r.indicators.isolated_letter_ratio
            + r.indicators.vowelless_word_ratio
            + r.indicators.runaway_token_ratio
        ),
        reverse=True,
    )[:10]
    lines += ["", "  worst ten (by indicators tripped, not by severity — there is no scale)"]
    for row in worst:
        i = row.indicators
        lines.append(
            f"  {row.chunk_id[:8]}  {row.method:<10} isolated={i.isolated_letter_ratio:.3f} "
            f"vowelless={i.vowelless_word_ratio:.3f} runaway={i.runaway_token_ratio:.3f} "
            f"repl={i.replacement_chars} ctrl={i.control_chars}"
        )

    clean = sum(1 for r in rows if not r.indicators.any_indicator)
    lines += [
        "",
        f"  no indicator      {clean} of {len(rows)} chunks",
        "  That is the absence of evidence, not evidence of absence. Two columns interleaved",
        "  line by line produce flawless words in meaningless order and trip none of these.",
        "",
        "  No threshold is applied and none is set. What counts as too high here has never",
        "  been measured, and choosing a number before seeing a real distribution is the",
        "  guess S18 exists to count.",
    ]
    return "\n".join(lines) + "\n"


async def collect(limit: int | None) -> list[Row]:
    statement = select(Chunk).order_by(Chunk.created_at)
    if limit is not None:
        statement = statement.limit(limit)
    rows: list[Row] = []
    async with SessionFactory() as session:
        for chunk in (await session.scalars(statement)).all():
            raw = (chunk.provenance or {}).get("extraction")
            if not isinstance(raw, dict):
                continue  # predates the measurement; counted by its absence, not invented
            rows.append(
                Row(
                    chunk_id=str(chunk.id),
                    method=str((chunk.provenance or {}).get("method", "unknown")),
                    indicators=ExtractionIndicators.model_validate(raw),
                )
            )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Extraction-damage indicators over the corpus (S27).")
    ap.add_argument("--limit", type=int, default=None, help="only the oldest N chunks")
    args = ap.parse_args()
    print()
    print(render(asyncio.run(collect(args.limit))))


if __name__ == "__main__":
    main()
