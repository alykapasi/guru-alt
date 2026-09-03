"""The kc_tagging classification as a DSPy program (Phase 9c).

Runtime loads the committed compiled artifact (optimized instruction + few-shot demos); if it is
absent or unreadable, an uncompiled program seeded with the original hand-written instruction runs
instead — so ingestion always has a working tagger. This module is the ONLY user of dspy in the
kc_tagging path; app/learning/kc_tagging.py talks to it, not to dspy.
"""

from __future__ import annotations

from pathlib import Path

import dspy
import structlog
from pydantic import BaseModel

log = structlog.get_logger(__name__)

KC_TAGGING_INSTRUCTION = (
    "You label a passage with the knowledge components (KCs) it actually teaches. You are given "
    "a numbered list of candidate KCs and a passage. Choose only the KCs the passage directly "
    "teaches or assesses — usually zero to three; omit tangential mentions. Return the candidate "
    "number and your confidence (0.0-1.0) for each chosen KC; return an empty list if none apply."
)

ARTIFACT_PATH = Path(__file__).parent / "artifacts" / "kc_tagging.json"


class TagPrediction(BaseModel):
    kc: int
    confidence: float


class TagPassage(dspy.Signature):
    """Label a passage with the candidate KCs it teaches."""

    passage: str = dspy.InputField()
    candidates: str = dspy.InputField(desc="numbered candidate KCs, one per line")
    tags: list[TagPrediction] = dspy.OutputField(desc="chosen candidate numbers with confidence")


class KCTaggingProgram(dspy.Module):
    def __init__(self) -> None:
        super().__init__()
        self.tag = dspy.Predict(TagPassage.with_instructions(KC_TAGGING_INSTRUCTION))

    def forward(self, passage: str, candidates: str):
        return self.tag(passage=passage, candidates=candidates)

    async def aforward(self, passage: str, candidates: str):
        return await self.tag.acall(passage=passage, candidates=candidates)


def load_kc_tagging_program() -> KCTaggingProgram:
    program = KCTaggingProgram()
    if ARTIFACT_PATH.exists():
        try:
            program.load(str(ARTIFACT_PATH))
        except Exception as exc:  # corrupt/incompatible artifact — fall back, never fail ingest
            log.warning("kc_tagging.artifact_load_failed", path=str(ARTIFACT_PATH), error=str(exc))
    return program
