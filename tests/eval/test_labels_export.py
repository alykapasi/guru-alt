"""Labelled examples export as traceable JSONL that git never sees (S59)."""

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import EvalExample
from app.models.learner import Learner
from app.workers import eval_export
from tests.eval.labels.metrics import MIN_READABLE
from tests.eval.labels.report import _pair, render_area


async def _example(session: AsyncSession, *, label: dict | None) -> EvalExample:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    example = EvalExample(
        area="intent",
        learner_id=learner.id,
        source_kind="intent",
        source_id=uuid.uuid4(),
        snapshot={"stem": "Q", "message": "A"},
        decision={"intent": "attempt"},
        label=label,
        labelled_at=datetime.now(UTC) if label else None,
    )
    session.add(example)
    await session.flush()
    return example


async def test_only_labelled_examples_are_written(db_session: AsyncSession, tmp_path: Path) -> None:
    kept = await _example(db_session, label={"intent": "deferral"})
    await _example(db_session, label=None)
    path = tmp_path / "intent.jsonl"
    assert await eval_export.export(db_session, "intent", path) == 1
    (line,) = path.read_text().splitlines()
    row = json.loads(line)
    assert row["id"] == str(kept.id)
    assert row["source_kind"] == "intent" and row["source_id"] == str(kept.source_id)
    assert row["decision"] == {"intent": "attempt"} and row["label"] == {"intent": "deferral"}
    assert row["snapshot"] == {"stem": "Q", "message": "A"}


def test_the_private_directory_is_gitignored() -> None:
    target = "tests/eval/datasets/private/intent.jsonl"
    result = subprocess.run(["git", "check-ignore", target], capture_output=True, text=True)
    assert result.returncode == 0, f"{target} is NOT gitignored"


def test_the_default_path_is_the_private_directory() -> None:
    assert eval_export.default_path("grading").parts[-3:] == (
        "datasets",
        "private",
        "grading.jsonl",
    )


def test_too_few_labels_are_not_read() -> None:
    pairs = [("attempt", "attempt")] * (MIN_READABLE - 1)
    assert "too few to read" in render_area("intent", pairs)


def test_enough_labels_are_read_and_grading_states_its_bands() -> None:
    pairs = [("pass", "pass")] * 15 + [("fail", "partial")] * 5
    text = render_area("grading", pairs)
    assert "too few to read" not in text
    assert "accuracy 0.750" in text
    assert "fail < 0.4 ≤ partial < 0.8 ≤ pass" in text


def test_source_support_is_read_as_a_table_of_coverage_against_support() -> None:
    # The coverage label is a rule, not a judgement: what is measured is whether a reply that
    # carried "cited" was in fact supported, so there is no agreement figure to print.
    pairs = (
        [("cited", "supported")] * 15
        + [("cited", "unsupported")] * 3
        + [("retrieved_not_cited", "partly")] * 2
    )
    text = render_area("source_support", pairs)
    assert "kappa" not in text and "accuracy" not in text
    assert "cited" in text and "supported 15" in text and "unsupported 3" in text
    assert "cited replies judged supported: 15 of 18" in text


def test_a_source_support_example_pairs_its_coverage_with_the_judged_support() -> None:
    example = EvalExample(
        area="source_support",
        decision={"coverage": "cited", "offered": 2},
        label={"support": "unsupported"},
    )
    assert _pair("source_support", example) == ("cited", "unsupported")
