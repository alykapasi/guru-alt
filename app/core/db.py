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
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


engine: AsyncEngine = create_engine(get_settings())
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a per-request session."""
    async with SessionFactory() as session:
        yield session


async def release_before_model_wait(session: AsyncSession) -> None:
    """End ``session``'s transaction, so it holds no pool connection while a model streams.

    A transaction keeps its connection until it ends, and a streamed reply can run until the
    turn's deadline: each learner waiting on one would hold a connection the rest of the app
    needs. What was written so far is committed, so it stays even if the learner presses Stop.
    Loaded objects stay usable (``expire_on_commit=False``), and the next query starts a new
    transaction on a fresh connection.
    """
    if session.in_transaction():
        await session.commit()


def engine_of(session: AsyncSession) -> AsyncEngine:
    """The engine behind ``session``, for work that needs its own connection to the same
    database (advisory locks: ingestion slots, turn claims)."""
    bind = session.bind
    return bind if isinstance(bind, AsyncEngine) else bind.engine
