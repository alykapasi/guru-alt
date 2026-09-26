"""Building a decision runtime for a test. Not ``test_``-prefixed, so it is not collected."""

from collections.abc import Iterator
from contextlib import contextmanager

from app.core.config import DecisionMode
from app.llm.decisions import DecisionClient
from app.services.decisions import DecisionPolicy, DecisionRuntime, set_runtime


def runtime(
    client: DecisionClient | None,
    *,
    intent: DecisionMode = "off",
    fully_correct: DecisionMode = "off",
    background: bool = False,
    intent_threshold: float = 0.9,
    fully_correct_threshold: float = 0.9,
    live_deadline_s: float = 0.8,
    shadow_timeout_s: float = 5.0,
) -> DecisionRuntime:
    """A runtime with shadow writes *inline* by default: a background write would share the
    test's database connection with the code under test while both are running."""
    return DecisionRuntime(
        client=client,
        policy=DecisionPolicy(
            intent_mode=intent,
            fully_correct_mode=fully_correct,
            intent_threshold=intent_threshold,
            fully_correct_threshold=fully_correct_threshold,
            live_deadline_s=live_deadline_s,
            shadow_timeout_s=shadow_timeout_s,
            background=background,
        ),
    )


@contextmanager
def using(decision_runtime: DecisionRuntime) -> Iterator[None]:
    previous = set_runtime(decision_runtime)
    try:
        yield
    finally:
        set_runtime(previous)
