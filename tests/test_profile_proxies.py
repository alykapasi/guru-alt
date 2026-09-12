"""Profile dimensions are described as the proxies they are, not as measured traits (S44).

Reading level was a readability score of the learner's own chat messages — a measure of how
they write to a tutor, not how well they read — and it was fed into note generation as "write
at roughly this reading level", so short casual questions asked for simpler explanations.
Cognitive-load tolerance was a within-session score drift over questions whose difficulty is
not held constant. Format effectiveness was a mean score per question format, with no matching
on difficulty or topic, and the planner routed the learner to whichever scored highest.
"""

import inspect

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import note_distill
from app.learning.profile_estimators import DIMENSION_SPECS, describe
from app.models.learner import Learner
from app.models.profile import LearnerProfile, ProfileDimension

API = "/api/v1"


def test_every_dimension_says_what_was_observed() -> None:
    for spec in DIMENSION_SPECS:
        assert spec.label and not spec.label.endswith("."), spec.key
        assert spec.observation.endswith("."), spec.key
        # The label is for a learner to read, so it must not just be the key back again.
        assert spec.label.lower() != spec.key.replace("_", " "), spec.key


def test_no_dimension_is_named_as_a_trait_it_does_not_measure() -> None:
    """The three names the review named. Renamed, not dropped — the measurements are still
    worth showing, under names that say what they are."""
    keys = {spec.key for spec in DIMENSION_SPECS}
    assert "reading_level" not in keys
    assert "cognitive_load_tolerance" not in keys
    assert "format_effectiveness" not in keys
    assert {
        "message_writing_complexity",
        "within_session_accuracy_drift",
        "score_by_format",
    } <= keys


def test_a_dimension_no_longer_in_the_catalog_still_renders() -> None:
    """Rows outlive the catalog — dropping a dimension is a code change with no migration."""
    label, observation = describe("some_retired_dimension")
    assert label == "Some retired dimension"
    assert observation


def test_generated_notes_are_not_written_to_the_learners_message_complexity() -> None:
    """The inference drove content: casual short questions instructed the tutor to simplify."""
    for fn in (note_distill.distill, note_distill.render):
        assert "reading_level" not in inspect.signature(fn).parameters


async def test_the_profile_endpoint_describes_each_dimension(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    # Seeded on the learner the API client is signed in as, so the endpoint sees it (S21).
    learner = api_learner
    db_session.add(LearnerProfile(learner_id=learner.id))
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="message_writing_complexity",
            value=8.5,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.commit()

    r = await api_client.get(f"{API}/profile")

    assert r.status_code == 200
    dimension = next(d for d in r.json()["dimensions"] if d["key"] == "message_writing_complexity")
    assert dimension["label"] == "Writing complexity of your messages"
    # The client is told what the number is not evidence of, not left to read the key.
    assert "not how well you read" in dimension["observation"]
