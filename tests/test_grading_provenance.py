"""A grade says what produced it (S56)."""

import json
import uuid

from app.learning import rubric_grading
from app.learning.grading import auto_grade, grade_flashcard
from app.learning.rubric_grading import GradedComponent, grade_open
from app.learning.turn_read import FULLY_CORRECT, ReadContext
from app.llm.decisions import FakeDecisionClient, YesNoAnswer
from app.llm.registry import fake_llm_client
from app.models.assessment import ItemType
from app.services import decisions
from tests.decision_support import runtime, using

GRADE = json.dumps({"score": 0.8, "rationale": "ok"})


def test_auto_and_self_graders_name_themselves() -> None:
    mcq = auto_grade(ItemType.MCQ, {"correct": 1}, {"choice": 1})
    assert mcq.provenance is not None and mcq.provenance.grader == "auto"
    card = grade_flashcard({"rating": 3})
    assert card.provenance is not None and card.provenance.grader == "self"


async def test_the_rubric_grader_records_its_prompt_and_model() -> None:
    result, _ = await grade_open(
        fake_llm_client(GRADE), stem="Q", response={"text": "a"}, rubric=None
    )
    p = result.provenance
    assert p is not None and p.grader == "rubric"
    assert p.system_prompt == rubric_grading._SYSTEM_PROMPT
    assert p.template_version == rubric_grading.TEMPLATE_VERSION
    assert p.model == "fake:fake-1"


async def test_the_component_prompt_is_the_one_recorded_for_a_multi_component_item() -> None:
    comps = [GradedComponent(kc_id=uuid.uuid4(), name=n) for n in ("A", "B")]
    reply = json.dumps({"components": [{"n": 1, "score": 1}, {"n": 2, "score": 0}], "score": 0.5})
    result, _ = await grade_open(
        fake_llm_client(reply), stem="Q", response={"text": "a"}, rubric=None, components=comps
    )
    assert result.provenance is not None
    assert result.provenance.system_prompt == rubric_grading._COMPONENT_SYSTEM_PROMPT


async def test_an_empty_answer_names_the_grader_but_no_prompt_or_model() -> None:
    result, _ = await grade_open(
        fake_llm_client(GRADE), stem="Q", response={"text": "  "}, rubric=None
    )
    assert result.provenance is not None
    assert (result.provenance.grader, result.provenance.system_prompt, result.provenance.model) == (
        "rubric",
        None,
        None,
    )


async def test_a_system_override_is_what_is_sent_and_recorded() -> None:
    llm = fake_llm_client(GRADE)
    result, _ = await grade_open(
        llm, stem="Q", response={"text": "a"}, rubric=None, system="OLD PROMPT"
    )
    assert result.provenance is not None and result.provenance.system_prompt == "OLD PROMPT"


async def test_a_live_jev_pass_names_jev(db_session) -> None:
    fake = FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.99)})

    async def smart():
        raise AssertionError("skipped")

    with using(runtime(fake, fully_correct="live")):
        result = await decisions.decide_grade(
            smart=smart,
            stem="q",
            answer="a",
            rubric_criteria=None,
            context=ReadContext(learner_id=None, conversation_id=None, item_id=None),
            attempt_id=None,
        )
    assert result.provenance is not None and result.provenance.grader == "jev"
