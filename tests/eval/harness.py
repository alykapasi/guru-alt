"""The eval-harness seed (TECHNICAL_DESIGN §10, ROADMAP Phase 3).

*Evals are not unit tests.* A unit test asserts one code path is correct; an eval scores
the engine's **behavioral quality** over a **dataset of golden cases** and gates on an
aggregate metric. The cases live as data (``cases/*.json``) so the set grows by adding
rows, not code — this is exactly the substrate DSPy optimizes against and that becomes a
release gate later (Phase 8).

Three suites seed it: deterministic **grading** reliability, **tracer** sanity (mastery
monotonicity, convergence, roll-up), and LLM **rubric** grading reliability (run against a
live model). The first two are offline + deterministic (CI gate); rubric needs a model.
"""

import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.grading import auto_grade, grade_flashcard
from app.learning.kc_tagging import KCCandidate, tag_chunk
from app.learning.rubric_grading import RubricGradingError, grade_open
from app.learning.tracer import (
    DEFAULT_ABILITY,
    DEFAULT_UNCERTAINTY,
    Estimate,
    GlickoEstimator,
    aggregate,
)
from app.llm import LLMClient, ModelRole
from app.llm.registry import fake_llm_client
from app.models.assessment import AUTO_GRADABLE, SELF_GRADABLE, ItemType, Rubric
from app.models.content import ContentType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import retrieval
from app.services import content as content_svc

CASES_DIR = Path(__file__).parent / "cases"


# --- case + report models ---------------------------------------------------


class GradingCase(BaseModel):
    """One golden grading case: a response that should grade to ``expected_score``."""

    id: str
    item_type: ItemType
    response: dict
    expected_score: float
    answer_key: dict | None = None
    tolerance: float = 0.001


class RubricCase(BaseModel):
    """A golden rubric case: a human-scored open response (graded by a live model)."""

    id: str
    stem: str
    response: dict
    expected_score: float
    criteria: dict = Field(default_factory=dict)
    tolerance: float = 0.25  # looser — a real model isn't deterministic


class TracerStep(BaseModel):
    score: float
    difficulty: float = 0.0


class ChildEstimate(BaseModel):
    ability: float
    uncertainty: float
    weight: float = 1.0


class TracerCase(BaseModel):
    """A tracer-sanity case: replay observations (or aggregate children) and assert a property."""

    id: str
    kind: Literal["sequence", "rollup"]
    property: str
    threshold: float | None = None
    steps: list[TracerStep] = Field(default_factory=list)
    children: list[ChildEstimate] = Field(default_factory=list)


class RetrievalDoc(BaseModel):
    """A corpus document for a retrieval case (a single chunk's worth of text)."""

    id: str  # local handle, referenced by ``expect_top``
    text: str


class RetrievalCase(BaseModel):
    """A golden retrieval case: ``query`` should surface ``expect_top`` within the top ``k``.

    Each case carries its own small corpus, so cases are independent. Queries share lexical
    signal with their target (the keyword path is genuinely semantic; fake embeddings are not),
    which makes recall@k deterministic and CI-gatable.
    """

    id: str
    query: str
    corpus: list[RetrievalDoc]
    expect_top: list[str]
    k: int = 3


class GroundingCase(BaseModel):
    """A grounding-sanity case: a (scripted) model cites ``cite`` indices over a seeded corpus.

    Proves the citation contract — a block cites **only real retrieved chunks**, dropping any
    out-of-range index — independent of model quality.
    """

    id: str
    kc_name: str
    corpus: list[str]
    cite: list[int]


class KCTaggingCase(BaseModel):
    """A golden KC-tagging case: ``text`` should be tagged with the ``expect`` candidate KC(s).

    ``candidates`` are KC names shown to the tagger in order; ``expect`` holds the 1-based
    indices of the KC(s) the passage actually teaches (empty ⇒ it teaches none of them).
    """

    id: str
    text: str
    candidates: list[str]
    expect: list[int] = Field(default_factory=list)


class CaseResult(BaseModel):
    case_id: str
    passed: bool
    detail: str = ""


class EvalReport(BaseModel):
    """Per-case results plus the aggregate metrics a gate reads."""

    suite: str
    results: list[CaseResult]
    mae: float | None = None  # mean absolute error, for score-based suites

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def summary(self) -> str:
        mae = f", MAE={self.mae:.3f}" if self.mae is not None else ""
        return f"{self.suite}: {self.passed}/{self.total} passed ({self.pass_rate:.0%}){mae}"


# --- loaders ----------------------------------------------------------------


def _load[CaseT: BaseModel](name: str, model: type[CaseT]) -> list[CaseT]:
    data = json.loads((CASES_DIR / name).read_text())
    return [model.model_validate(row) for row in data]


def load_grading_cases() -> list[GradingCase]:
    return _load("grading.json", GradingCase)


def load_rubric_cases() -> list[RubricCase]:
    return _load("rubric.json", RubricCase)


def load_tracer_cases() -> list[TracerCase]:
    return _load("tracer.json", TracerCase)


def load_retrieval_cases() -> list[RetrievalCase]:
    return _load("retrieval.json", RetrievalCase)


def load_grounding_cases() -> list[GroundingCase]:
    return _load("grounding.json", GroundingCase)


def load_kc_tagging_cases() -> list[KCTaggingCase]:
    return _load("kc_tagging.json", KCTaggingCase)


# --- scorers ----------------------------------------------------------------


def score_grading(cases: Sequence[GradingCase]) -> EvalReport:
    """Score deterministic grading (auto + self). No model, no DB."""
    results: list[CaseResult] = []
    errors: list[float] = []
    for case in cases:
        if case.item_type in AUTO_GRADABLE:
            score = auto_grade(case.item_type, case.answer_key or {}, case.response).score
        elif case.item_type in SELF_GRADABLE:
            score = grade_flashcard(case.response).score
        else:  # pragma: no cover - guarded by dataset authoring
            results.append(CaseResult(case_id=case.id, passed=False, detail="not deterministic"))
            continue
        err = abs(score - case.expected_score)
        errors.append(err)
        results.append(
            CaseResult(
                case_id=case.id,
                passed=err <= case.tolerance,
                detail=f"got {score:.3f}, want {case.expected_score:.3f}",
            )
        )
    mae = sum(errors) / len(errors) if errors else None
    return EvalReport(suite="grading", results=results, mae=mae)


async def score_rubric(client: LLMClient, cases: Sequence[RubricCase]) -> EvalReport:
    """Score LLM rubric grading against human labels — needs a live model."""
    results: list[CaseResult] = []
    errors: list[float] = []
    for case in cases:
        rubric = Rubric(criteria=case.criteria) if case.criteria else None
        try:
            result, _usage = await grade_open(
                client, stem=case.stem, response=case.response, rubric=rubric
            )
        except RubricGradingError as exc:
            # A weak model may emit unparseable output; record it, don't crash the suite.
            results.append(CaseResult(case_id=case.id, passed=False, detail=f"grade failed: {exc}"))
            continue
        err = abs(result.score - case.expected_score)
        errors.append(err)
        results.append(
            CaseResult(
                case_id=case.id,
                passed=err <= case.tolerance,
                detail=f"got {result.score:.3f}, want {case.expected_score:.3f}",
            )
        )
    mae = sum(errors) / len(errors) if errors else None
    return EvalReport(suite="rubric", results=results, mae=mae)


def score_tracer(cases: Sequence[TracerCase]) -> EvalReport:
    """Score tracer-sanity properties against the pure estimator + aggregation."""
    return EvalReport(suite="tracer", results=[_run_tracer_case(c) for c in cases])


async def score_retrieval(
    session: AsyncSession, llm: LLMClient, cases: Sequence[RetrievalCase]
) -> EvalReport:
    """Score hybrid retrieval recall@k over self-contained golden corpora (needs a DB)."""
    results: list[CaseResult] = []
    for case in cases:
        learner, by_doc = await _seed_corpus(session, llm, [(d.id, d.text) for d in case.corpus])
        hits = await retrieval.retrieve(
            session, llm, case.query, learner_id=learner.id, limit=case.k
        )
        top = {h.chunk_id for h in hits}
        want = {by_doc[doc_id] for doc_id in case.expect_top}
        found = want & top
        results.append(
            CaseResult(
                case_id=case.id,
                passed=found == want,
                detail=f"recall {len(found)}/{len(want)} in top-{case.k}",
            )
        )
    return EvalReport(suite="retrieval", results=results)


async def score_grounding(session: AsyncSession, cases: Sequence[GroundingCase]) -> EvalReport:
    """Score the citation contract: a generated block cites only real retrieved chunks."""
    results: list[CaseResult] = []
    for case in cases:
        client = fake_llm_client(
            reply=json.dumps({"body": f"{case.kc_name} explained.", "citations": case.cite})
        )
        learner, by_doc = await _seed_corpus(
            session, client, [(str(i), t) for i, t in enumerate(case.corpus)]
        )
        kc = await _seed_kc(session, case.kc_name)
        block = await content_svc.generate_block(
            session, client, learner_id=learner.id, kc_id=kc.id, block_type=ContentType.LESSON
        )
        cited = {c["chunk_id"] for c in block.citations}
        real = {str(cid) for cid in by_doc.values()}
        valid = {i for i in case.cite if 0 <= i < len(case.corpus)}
        passed = (
            block.kc_ids == [kc.id]  # KC-tagged
            and cited <= real  # cites only real retrieved chunks (no fabrication)
            and len(block.citations) == len(valid)  # out-of-range indices dropped, no dupes
        )
        results.append(
            CaseResult(
                case_id=case.id,
                passed=passed,
                detail=f"{len(cited)} citations, all real={cited <= real}",
            )
        )
    return EvalReport(suite="grounding", results=results)


async def score_kc_tagging(
    client: LLMClient, cases: Sequence[KCTaggingCase], *, min_confidence: float = 0.5
) -> EvalReport:
    """Score per-chunk KC auto-tagging against golden labels — needs a live model.

    For each case the candidate names become KCs, the passage is tagged, and the predicted KC
    set (as 1-based indices) is compared to the expected one. Passes on an exact set match. No
    DB: the tagger is pure over the candidate list.
    """
    results: list[CaseResult] = []
    for case in cases:
        candidates = [KCCandidate(id=uuid.uuid4(), name=name) for name in case.candidates]
        index_of = {c.id: i + 1 for i, c in enumerate(candidates)}
        tags, _usage = await tag_chunk(client, case.text, candidates, min_confidence=min_confidence)
        predicted = {index_of[t.kc_id] for t in tags}
        expected = set(case.expect)
        results.append(
            CaseResult(
                case_id=case.id,
                passed=predicted == expected,
                detail=f"predicted {sorted(predicted)}, want {sorted(expected)}",
            )
        )
    return EvalReport(suite="kc_tagging", results=results)


async def _seed_corpus(
    session: AsyncSession, llm: LLMClient, docs: Sequence[tuple[str, str]]
) -> tuple[Learner, dict[str, uuid.UUID]]:
    """Create a fresh learner + one source and embed ``docs`` (handle, text) into chunks."""
    learner = Learner(handle=f"eval-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="eval.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        meta={},
    )
    session.add(source)
    await session.flush()

    vectors = (await llm.embed(ModelRole.EMBED, [text for _, text in docs])).vectors
    by_doc: dict[str, uuid.UUID] = {}
    for ordinal, ((doc_id, text), vector) in enumerate(zip(docs, vectors, strict=True)):
        chunk = Chunk(
            source_id=source.id,
            ordinal=ordinal,
            text=text,
            embedding=vector,
            provenance={"source_id": str(source.id), "method": "text"},
        )
        session.add(chunk)
        await session.flush()
        by_doc[doc_id] = chunk.id
    return learner, by_doc


async def _seed_kc(session: AsyncSession, name: str) -> KC:
    subject = Subject(slug=f"eval-{uuid.uuid4().hex[:8]}", name="Eval")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"eval-{uuid.uuid4().hex[:8]}", name="Eval")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"eval-{uuid.uuid4().hex[:8]}", name=name)
    session.add(kc)
    await session.flush()
    return kc


def _run_tracer_case(case: TracerCase) -> CaseResult:
    if case.kind == "sequence":
        estimator = GlickoEstimator()
        est = Estimate()
        for step in case.steps:
            est = estimator.update(est, score=step.score, difficulty=step.difficulty)
        passed, detail = _check_sequence(case.property, est, case.threshold)
    else:
        estimates = [Estimate(ability=c.ability, uncertainty=c.uncertainty) for c in case.children]
        parent = aggregate(estimates, [c.weight for c in case.children])
        passed, detail = _check_rollup(case.property, parent, estimates)
    return CaseResult(case_id=case.id, passed=passed, detail=detail)


def _check_sequence(prop: str, final: Estimate, threshold: float | None) -> tuple[bool, str]:
    checks = {
        "ability_up": final.ability > DEFAULT_ABILITY,
        "ability_down": final.ability < DEFAULT_ABILITY,
        "uncertainty_down": final.uncertainty < DEFAULT_UNCERTAINTY,
        "ability_at_least": threshold is not None and final.ability >= threshold,
        "ability_at_most": threshold is not None and final.ability <= threshold,
    }
    return _verdict(prop, checks, f"ability={final.ability:.3f}, sd={final.uncertainty:.3f}")


def _check_rollup(prop: str, parent: Estimate, children: Sequence[Estimate]) -> tuple[bool, str]:
    abilities = [c.ability for c in children]
    uncertainties = [c.uncertainty for c in children]
    checks = {
        # A (precision-)weighted mean always lands within the children's range.
        "ability_between": bool(abilities) and min(abilities) <= parent.ability <= max(abilities),
        # An untested (wide) child widens the parent above the tested child's certainty.
        "uncertainty_widened": bool(uncertainties) and parent.uncertainty > min(uncertainties),
    }
    return _verdict(prop, checks, f"ability={parent.ability:.3f}, sd={parent.uncertainty:.3f}")


def _verdict(prop: str, checks: dict[str, bool], detail: str) -> tuple[bool, str]:
    if prop not in checks:
        return False, f"unknown property {prop!r}"
    return checks[prop], f"{prop}: {detail}"


# --- standalone runner (`poe eval`) -----------------------------------------


def run_offline() -> list[EvalReport]:
    """Run the deterministic suites (grading + tracer) and return their reports."""
    return [score_grading(load_grading_cases()), score_tracer(load_tracer_cases())]


def main() -> int:
    """Print the offline eval report; exit non-zero if any suite is below 100%."""
    reports = run_offline()
    ok = True
    for report in reports:
        print(report.summary())
        for result in report.results:
            if not result.passed:
                ok = False
                print(f"  FAIL {result.case_id}: {result.detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
