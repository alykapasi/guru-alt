"""v1 API routers."""

from fastapi import APIRouter

from app.api.v1 import knowledge

api_router = APIRouter()
api_router.include_router(knowledge.router)
