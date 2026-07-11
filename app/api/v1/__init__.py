"""v1 API routers."""

from fastapi import APIRouter

from app.api.v1 import assessment, chat, content, knowledge, placement, sources

api_router = APIRouter()
api_router.include_router(knowledge.router)
api_router.include_router(chat.router)
api_router.include_router(assessment.router)
api_router.include_router(sources.router)
api_router.include_router(content.router)
api_router.include_router(placement.router)
