"""Run one Cell: build its client, run its suites over the golden cases, price the calls."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from tests.eval import harness
from tests.eval.sweep.config import Cell
from tests.eval.sweep.cost import CostSummary, CostTrackingClient


async def run_cell(
    cell: Cell,
    base_client: LLMClient,
    *,
    session: AsyncSession | None = None,
) -> tuple[list[harness.EvalReport], CostSummary]:
    """Run ``cell``'s suites against a fresh, cost-tracked client; return reports + cost.

    ``base_client`` supplies the providers + ambient role map; the cell's ``role_overrides`` are
    merged over it (never mutating ``base_client``). A ``retrieval`` suite needs ``session``.
    """
    client = CostTrackingClient(base_client.with_roles(cell.role_overrides))
    reports = [await _run_suite(suite, client, session, cell.toggles) for suite in cell.suites]
    return reports, client.cost_summary()


async def _run_suite(
    suite: str,
    client: LLMClient,
    session: AsyncSession | None,
    toggles: dict[str, bool],
) -> harness.EvalReport:
    if suite == "rubric":
        return await harness.score_rubric(client, harness.load_rubric_cases())
    if suite == "kc_tagging":
        # Concrete toggle wired end-to-end: `strict_kc_tagging` raises the confidence gate
        # (flips the scorer's existing `min_confidence` seam — no app-code change).
        min_confidence = 0.7 if toggles.get("strict_kc_tagging") else 0.5
        return await harness.score_kc_tagging(
            client, harness.load_kc_tagging_cases(), min_confidence=min_confidence
        )
    if suite == "retrieval":
        if session is None:
            raise ValueError("the 'retrieval' suite requires a database session")
        return await harness.score_retrieval(session, client, harness.load_retrieval_cases())
    raise ValueError(f"unknown or non-sweepable suite {suite!r}")
