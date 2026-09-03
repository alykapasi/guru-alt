"""kc_tagging DSPy program + artifact loader (Phase 9c)."""

import dspy

from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.prompts.kc_tagging_program import (
    KCTaggingProgram,
    TagPrediction,
    load_kc_tagging_program,
)
from app.prompts.lm import RoleLM


def test_load_falls_back_to_uncompiled_when_artifact_absent() -> None:
    # No committed artifact in CI -> a usable uncompiled program, no raise.
    program = load_kc_tagging_program()
    assert isinstance(program, KCTaggingProgram)


def test_program_runs_and_yields_tag_predictions() -> None:
    reply = '[[ ## tags ## ]]\n[{"kc": 1, "confidence": 0.9}]\n\n[[ ## completed ## ]]'
    lm = RoleLM(ModelRole.FAST, fake_llm_client(reply=reply))
    with dspy.context(lm=lm):
        pred = KCTaggingProgram()(passage="Photosynthesis...", candidates="1. Photosynthesis")
    assert isinstance(pred.tags, list)
    assert pred.tags and isinstance(pred.tags[0], TagPrediction)
    assert pred.tags[0].kc == 1
