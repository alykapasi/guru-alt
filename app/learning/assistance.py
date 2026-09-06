"""How much *independent* evidence one attempt carries (TECHNICAL_DESIGN §7.2, S13).

Mastery is a claim about what a learner can do unaided. An attempt made after hints, or after
already seeing the question and being told how it went, is not that claim: it is a noisier
measurement of it. Guided practice hints and re-asks the *same* problem until the learner gets
it, so without this every scaffolded round fed the tracer a full-strength observation and three
rounds of help could look like three independent demonstrations.

The correction is deliberately symmetric. Scaffolding does not mean "give less credit for
success" — it means the attempt tells us less, in either direction, so it moves ability less
*and* shrinks uncertainty less. An assisted success no longer manufactures confidence, and an
assisted failure no longer condemns a learner who was mid-explanation.

Assistance is not scored: it is counted. Each scaffold — a hint, or a prior look at this same
question in this sitting — halves, then thirds, then quarters the evidence, and never reaches
zero, because even a heavily helped attempt says something.
"""


def evidence_credit(*, hints_used: int | None = None, prior_attempts: int = 0) -> float:
    """Fraction of a full observation's weight an attempt earns, in (0, 1].

    ``1 / (1 + scaffolds)``: unaided attempts count fully, one scaffold halves the evidence,
    two thirds it, and so on. Hints and prior exposures both count — in guided practice they
    usually arrive together, and they weaken independence for different reasons (being told
    part of the answer versus having already been told the answer was wrong).
    """
    scaffolds = max(hints_used or 0, 0) + max(prior_attempts, 0)
    return 1.0 / (1.0 + scaffolds)
