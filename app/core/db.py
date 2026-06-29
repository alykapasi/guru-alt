"""Async database engine, session factory, and the ORM declarative base.

The engine is created once at import. Request handlers depend on :func:`get_session`
to get a per-request ``AsyncSession``. ORM models (Phase 1+) inherit from :class:`Base`,
whose ``metadata`` is the Alembic autogenerate target.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def create_engine(settings: Settings) -> AsyncEngine:
    """Build an async engine from settings."""
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
    )


engine: AsyncEngine = create_engine(get_settings())
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a per-request session."""
    async with SessionFactory() as session:
        yield session
