"""The learning engine: knowledge graph, mastery tracer, FSRS, profile, policy.

This package is the education core (see docs/MASTERPLAN §4 and TECHNICAL_DESIGN §7).
It is layered so the swappable pieces — the mastery estimator, later the learner
profile — sit behind small interfaces and carry no I/O of their own; persistence and
orchestration wrap them in the service layer.
"""
