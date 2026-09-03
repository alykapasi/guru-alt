"""`poe prompt-report kc_tagging` — measure compiled vs. uncompiled on the held-out dev set (9c).

Paid/manual. Evaluates the uncompiled program (baseline) and the compiled artifact (candidate) on
the dev split with the exact-set-match metric, logs both to MLflow (9a Tracker), prints the delta.
"""

from __future__ import annotations

import sys

import dspy

from app.core.config import get_settings
from app.llm import ModelRole
from app.llm.registry import build_llm_client
from app.prompts.kc_tagging_program import KCTaggingProgram, load_kc_tagging_program
from app.prompts.lm import RoleLM
from tests.eval.prompts.metric import kc_set_match
from tests.eval.prompts.trainset import load_kc_tagging_examples
from tests.eval.sweep.tracking import MLflowTracker


def format_delta(*, baseline: float, candidate: float) -> str:
    return (
        f"kc_tagging exact-match  baseline(uncompiled)={baseline:.3f}  "
        f"compiled={candidate:.3f}  delta={candidate - baseline:+.3f}"
    )


def _score(program, dev, lm) -> float:
    with dspy.context(lm=lm):
        passed = sum(
            bool(kc_set_match(ex, program(passage=ex.passage, candidates=ex.candidates)))
            for ex in dev
        )
    return passed / len(dev) if dev else 0.0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "kc_tagging":
        print("usage: poe prompt-report kc_tagging", file=sys.stderr)
        return 2
    _train, dev = load_kc_tagging_examples()
    lm = RoleLM(ModelRole.FAST, build_llm_client(get_settings()))
    baseline = _score(KCTaggingProgram(), dev, lm)
    candidate = _score(load_kc_tagging_program(), dev, lm)
    tracker = MLflowTracker(experiment="prompt-kc_tagging")
    for name, score in (("uncompiled", baseline), ("compiled", candidate)):
        tracker.start_run(name)
        tracker.log_metrics({"kc_tagging_exact_match": score})
        tracker.end_run()
    print(format_delta(baseline=baseline, candidate=candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
