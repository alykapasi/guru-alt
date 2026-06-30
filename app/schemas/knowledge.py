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
