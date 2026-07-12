"""Request/response schemas for the placement diagnostic."""

from pydantic import BaseModel, Field

from app.schemas.assessment import ItemRead, KCEstimateRead


class PlacementPromptRead(BaseModel):
    question: str


class PlacementSubmit(BaseModel):
    background: str = Field(min_length=1)


class PlacementResultRead(BaseModel):
    seeded: list[KCEstimateRead]
    light_test_items: list[ItemRead]
