"""Score the shipped estimator against alternatives on the same sequences (S56).

The reading S56 made possible and did not take. Replaying faithfully answers "did what we
shipped predict well?"; it cannot answer "would something else have done better?", and without
that second answer a calibration number has no scale. 0.87 skill is a good result or a poor one
only relative to what a plain Elo, or a forecaster that ignores the learner entirely, scores on
exactly the same steps.

Every candidate walks the *same* dataset through the same predict-then-update replay, so the
comparison holds everything constant except the estimator — the sequences, the order, the
elapsed gaps, the apportioned weights. The shipped row is the production replay with its
recorded predictions; candidate rows recompute every prediction, because the recorded ones are
not theirs (see ``tests.eval.datasets.calibration.replay``).
"""

from __future__ import annotations

from pydantic import BaseModel

from app.learning.tracer import GlickoEstimator, MasteryEstimator
from tests.eval.datasets.calibration import replay
from tests.eval.datasets.models import CalibrationDataset
from tests.eval.reliability import metrics
from tests.eval.reliability.candidates import ConstantEstimator, EloEstimator


class Scored(BaseModel):
    """One estimator's result on the shared dataset."""

    name: str
    config: dict[str, float]
    reliability: metrics.Reliability
    # True for the row replayed down the production path with the predictions production made
    # at the time, rather than recomputed. Exactly one row can carry it, and it is the only row
    # that is a statement about what shipped rather than about what a candidate would have done.
    as_shipped: bool = False

    @property
    def skill(self) -> float:
        return self.reliability.skill


class Comparison(BaseModel):
    """Every candidate on one dataset, best first."""

    n_steps: int
    rows: list[Scored]

    @property
    def best(self) -> Scored | None:
        return self.rows[0] if self.rows else None

    @property
    def shipped(self) -> Scored | None:
        return next((r for r in self.rows if r.as_shipped), None)

    @property
    def margin(self) -> float | None:
        """Shipped skill minus the best *candidate's*. Positive means production leads.

        Measured against the best alternative rather than against the best row overall, which
        would compare production with itself whenever production wins and report a lead of
        exactly zero. ``None`` when there is no shipped row or nothing to compare it with.
        """
        shipped = self.shipped
        others = [r for r in self.rows if not r.as_shipped]
        if shipped is None or not others:
            return None
        return shipped.skill - max(r.skill for r in others)

    @property
    def shipped_is_best(self) -> bool:
        return self.best is not None and self.best.as_shipped


def default_candidates(base_rate: float) -> list[MasteryEstimator]:
    """The alternatives worth a first look, given a dataset's own base rate.

    ``ConstantEstimator`` is built at the dataset's base rate rather than a fixed 0.5, so it is
    the null model the skill score is *defined* against. Its skill must come out at zero; when
    it does, the replay and the metrics agree about what they are measuring.
    """
    return [ConstantEstimator(p=base_rate), EloEstimator(k=0.4), EloEstimator(k=0.1)]


def compare(
    dataset: CalibrationDataset,
    *,
    candidates: list[MasteryEstimator] | None = None,
    buckets: int = metrics.DEFAULT_BUCKETS,
) -> Comparison | None:
    """Replay ``dataset`` through production and each candidate; score all of them alike.

    ``None`` when there is nothing to score. Rows come back best-skill first.
    """
    shipped_walk = replay(dataset)
    shipped = metrics.assess(shipped_walk.pairs, buckets=buckets)
    if shipped is None:
        return None

    chosen = candidates if candidates is not None else default_candidates(shipped.base_rate)
    rows = [
        Scored(
            name=GlickoEstimator.name,
            config=GlickoEstimator().config,
            reliability=shipped,
            as_shipped=True,
        )
    ]
    for candidate in chosen:
        scored = metrics.assess(replay(dataset, estimator=candidate).pairs, buckets=buckets)
        if scored is None:
            continue
        rows.append(Scored(name=candidate.name, config=candidate.config, reliability=scored))

    rows.sort(key=lambda r: r.skill, reverse=True)
    return Comparison(n_steps=shipped.n, rows=rows)
