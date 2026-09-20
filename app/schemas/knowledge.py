"""Request/response schemas for the knowledge graph."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

Slug = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")


class SubjectCreate(BaseModel):
    slug: str = Slug
    name: str = Field(min_length=1)
    description: str | None = None


class SubjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    # NULL means curated (S25). Safe to expose: a learner is only ever shown their own subjects
    # and curated ones, so the only non-NULL value that can reach them is their own id, which
    # they already have. It lets the app offer sharing on a subject that is theirs and not on
    # the shared library, instead of offering it everywhere and letting the API refuse.
    owner_learner_id: uuid.UUID | None = None
    # Exposed so the app can say *why* sharing is unavailable rather than offering an action
    # that answers 422 (S25b D4). It is not what enforces anything — the API refuses the
    # request whatever the browser renders — it is what keeps a learner from being shown a
    # door that refuses them. Same reasoning as `is_admin` on `LearnerRead`.
    private_source_derived: bool = False


class TopicCreate(BaseModel):
    slug: str = Slug
    name: str = Field(min_length=1)
    description: str | None = None


class TopicRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_id: uuid.UUID
    slug: str
    name: str
    description: str | None


class KCCreate(BaseModel):
    slug: str = Slug
    name: str = Field(min_length=1)
    description: str | None = None


class KCRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    topic_id: uuid.UUID
    slug: str
    name: str
    description: str | None


class PrerequisiteCreate(BaseModel):
    """Declare that ``prereq_kc_id`` is a prerequisite of the KC in the path."""

    prereq_kc_id: uuid.UUID
    weight: float = Field(default=1.0, gt=0.0)


class PrerequisiteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prereq_kc_id: uuid.UUID
    kc_id: uuid.UUID
    weight: float


class KCDetail(KCRead):
    """A KC plus its inbound prerequisite edges."""

    prerequisites: list[PrerequisiteRead] = []
