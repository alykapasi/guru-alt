"""`poe compile-prompt kc_tagging` — BootstrapFewShot-compile the kc_tagging program (Phase 9c).

Paid/slow/manual (needs a live model via the FAST role). Saves the optimized program (state-only
JSON) to app/prompts/artifacts/kc_tagging.json — commit it if `poe prompt-report` shows a win.
Not part of `poe check`/`poe eval`.
"""

from __future__ import annotations

import sys

import dspy

from app.core.config import get_settings
from app.llm import ModelRole
from app.llm.registry import build_llm_client
from app.prompts.kc_tagging_program import ARTIFACT_PATH, KCTaggingProgram
from app.prompts.lm import RoleLM
from tests.eval.prompts.metric import kc_set_match
from tests.eval.prompts.trainset import load_kc_tagging_examples


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "kc_tagging":
        print("usage: poe compile-prompt kc_tagging", file=sys.stderr)
        return 2
    train, _dev = load_kc_tagging_examples()
    lm = RoleLM(ModelRole.FAST, build_llm_client(get_settings()))
    with dspy.context(lm=lm):
        optimizer = dspy.BootstrapFewShot(metric=kc_set_match, max_bootstrapped_demos=4)
        compiled = optimizer.compile(KCTaggingProgram(), trainset=train)
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    compiled.save(str(ARTIFACT_PATH), save_program=False)
    print(f"compiled kc_tagging -> {ARTIFACT_PATH} ({len(train)} train examples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
