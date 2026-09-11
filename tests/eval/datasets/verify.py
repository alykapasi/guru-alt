"""`poe replay-check` — does replaying the live log reproduce the state we stored? (S56)

Manual; needs a DB. Reports fidelity, not accuracy: it says whether a replayed history lands
where the real one did. Until that is clean, any comparison between estimators is measuring
the harness as much as the model, so this runs *before* a model comparison, not after.

Exits non-zero when a sequence that claimed to be replayable failed to reproduce its recorded
state — that is a defect in the replay or in the log, and it should stop a pipeline.
"""

from __future__ import annotations

import asyncio

from app.core.db import SessionFactory
from tests.eval.datasets.mine import mine_observation_sequences
from tests.eval.datasets.replay import ReplayReport, verify_replay


async def _run() -> ReplayReport:
    async with SessionFactory() as session:
        dataset = await mine_observation_sequences(session)
    return verify_replay(dataset)


def _print(report: ReplayReport) -> None:
    if report.sequences_total == 0:
        print("no sequences yet — run some sessions first")
        return
    older = report.sequences_total - report.sequences_replayable
    print(f"sequences:   {report.sequences_total}")
    print(f"replayable:  {report.sequences_replayable}" + (f"  ({older} too old)" if older else ""))
    print(f"faithful:    {report.sequences_faithful}")
    if report.max_ability_error is not None:
        print(f"max error:   ability {report.max_ability_error:.3g}", end="")
        print(f", uncertainty {report.max_uncertainty_error:.3g}")
    for failure in report.failures:
        if not failure.replayable:
            continue
        worst = failure.worst
        where = f" at step {worst.index}" if worst else ""
        print(f"  MISMATCH {failure.kc_id} for {failure.learner_id}{where}")


def main() -> int:
    report = asyncio.run(_run())
    _print(report)
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
