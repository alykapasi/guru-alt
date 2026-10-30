"""``uv run poe label-report`` — the founder's labels against the models' decisions (S59).

Per area: how often the recorded decision matched the label, and Cohen's kappa, each with the
number of examples behind it. Below twenty it says so instead of printing a figure. With
``--replay`` it also re-runs today's prompts on the frozen inputs (locally, unless
``--allow-paid``) and prints that comparison beside the recorded one. No pass mark — setting
thresholds is S18's decision, after somebody has read this.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.llm.registry import build_llm_client
from app.models.evaluation import AREAS, EvalExample
from app.services.eval_areas import SUPPORT, VOCABULARY
from tests.eval.labels import injection as injection_cases
from tests.eval.labels.metrics import MIN_READABLE, categorical
from tests.eval.labels.replay import ROLES, PaidProviderRefused, current_decision, require_free

BANDS_NOTE = (
    "bands: fail < 0.4 ≤ partial < 0.8 ≤ pass (this report's convention, not the product's)"
)


def render_support(pairs: Sequence[tuple[str, str]]) -> str:
    """Coverage label against judged support: a table, not agreement — the two vocabularies
    differ, and the question is how often a reply that carried "cited" was in fact supported."""
    lines = ["source_support — coverage label vs judged support"]
    if len(pairs) < MIN_READABLE:
        lines.append(f"  n {len(pairs)}   too few to read (needs {MIN_READABLE})")
        return "\n".join(lines)
    lines.append(f"  n {len(pairs)}")
    for covered in sorted({c for c, _ in pairs}):
        judged = [s for c, s in pairs if c == covered]
        counts = "   ".join(f"{s} {judged.count(s)}" for s in SUPPORT if s in judged)
        lines.append(f"    {covered:<22} {counts}")
    cited = [s for c, s in pairs if c == "cited"]
    if cited:
        lines.append(
            f"  cited replies judged supported: {cited.count('supported')} of {len(cited)}"
        )
    return "\n".join(lines)


def render_area(area: str, pairs: Sequence[tuple[str, str]], *, heading: str = "recorded") -> str:
    if area == "source_support":
        return render_support(pairs)
    lines = [f"{area} — {heading} vs label"]
    if area == "grading":
        lines.append(f"  {BANDS_NOTE}")
    result = categorical(pairs)
    if result is None or result.n < MIN_READABLE:
        lines.append(f"  n {len(pairs)}   too few to read (needs {MIN_READABLE})")
        return "\n".join(lines)
    kappa = "undefined (one value throughout)" if result.kappa is None else f"{result.kappa:.3f}"
    lines.append(f"  n {result.n}   accuracy {result.accuracy:.3f}   kappa {kappa}")
    for (model, label), count in sorted(result.confusion.items()):
        mark = "" if model == label else "   ← differs"
        lines.append(f"    model {model:<22} label {label:<22} {count}{mark}")
    return "\n".join(lines)


def _pair(area: str, example: EvalExample) -> tuple[str, str] | None:
    key = VOCABULARY[area][0]
    decided = "coverage" if area == "source_support" else key
    model, label = example.decision.get(decided), (example.label or {}).get(key)
    return (str(model), str(label)) if model is not None and label is not None else None


async def run(
    areas: Sequence[str], *, replay: bool, allow_paid: bool, injection: bool = False
) -> str:
    out: list[str] = []
    async with SessionFactory() as session:
        llm = build_llm_client(get_settings()) if replay else None
        for area in areas:
            examples = (
                await session.scalars(
                    select(EvalExample).where(
                        EvalExample.area == area, EvalExample.label.isnot(None)
                    )
                )
            ).all()
            recorded = [p for e in examples if (p := _pair(area, e)) is not None]
            out.append(render_area(area, recorded))
            if replay and llm is not None and ROLES[area]:
                require_free(llm, ROLES[area], allow_paid=allow_paid)
                key = VOCABULARY[area][0]
                current: list[tuple[str, str]] = []
                for e in examples:
                    decided = await current_decision(llm, area, e.snapshot)
                    if decided is not None and e.label and key in e.label:
                        current.append((decided, str(e.label[key])))
                out.append(render_area(area, current, heading="current prompt"))
        if injection:
            if llm is None:
                out.append("injection — runs only with --replay")
            else:
                require_free(llm, [injection_cases.ROLE], allow_paid=allow_paid)
                out.append(
                    injection_cases.render(await injection_cases.run(llm, injection_cases.load()))
                )
    return "\n\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Founder labels against model decisions.")
    parser.add_argument("--area", choices=AREAS, action="append")
    parser.add_argument("--replay", action="store_true", help="re-run today's prompts locally")
    parser.add_argument("--allow-paid", action="store_true", help="let --replay spend money")
    parser.add_argument("--injection", action="store_true", help="with --replay: planted cases")
    args = parser.parse_args()
    try:
        print(
            asyncio.run(
                run(
                    args.area or list(AREAS),
                    replay=args.replay,
                    allow_paid=args.allow_paid,
                    injection=args.injection,
                )
            )
        )
    except PaidProviderRefused as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
