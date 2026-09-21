"""Evidence kinds (S56): a self-rating is evidence about retention, not about ability."""

import uuid

from app.learning.grading import auto_grade, grade_flashcard
from app.learning.mastery import Observation
from app.models.assessment import EvidenceKind, ItemType


def test_a_self_rated_flashcard_is_marked_self_reported() -> None:
    assert grade_flashcard({"rating": 4}).evidence_kind is EvidenceKind.SELF_REPORTED


def test_a_deterministically_graded_answer_is_marked_demonstrated() -> None:
    result = auto_grade(ItemType.MCQ, {"correct": 1, "choices": ["a", "b"]}, {"choice": 1})
    assert result.evidence_kind is EvidenceKind.DEMONSTRATED


def test_an_observation_is_demonstrated_unless_it_says_otherwise() -> None:
    """The default is the safe one: a caller that forgets the field asserts nothing extra.

    Inverted, a forgotten field would silently downgrade real evidence to self-report and
    stop the tracer learning from it — a failure that looks like nothing at all.
    """
    obs = Observation(learner_id=uuid.uuid4(), kc_weights={uuid.uuid4(): 1.0}, score=1.0)
    assert obs.evidence_kind is EvidenceKind.DEMONSTRATED


def test_a_client_cannot_claim_its_answer_was_demonstrated() -> None:
    """The kind is derived, never accepted. Pydantic ignores unknown fields by default, so
    without this test a future `model_config = {"extra": "allow"}` would silently hand the
    browser control of whether its own rating counts as evidence.
    """
    from app.schemas.assessment import AnswerSubmit

    submission = AnswerSubmit.model_validate(
        {"response": {"rating": 4}, "evidence_kind": "demonstrated"}
    )
    assert not hasattr(submission, "evidence_kind")
