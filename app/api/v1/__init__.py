"""v1 API routers."""

from fastapi import APIRouter

from app.api.v1 import chat, knowledge

api_router = APIRouter()
api_router.include_router(knowledge.router)
api_router.include_router(chat.router)
