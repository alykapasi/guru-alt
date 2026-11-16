"""The connection pool is sized by configuration (review, 2026-11-16).

A streamed tutor turn holds a connection for its turn lock besides its request session, so the
library default (5 + 10 overflow) is a concurrency ceiling a deployment has to be able to raise
without a code change.
"""

from app.core.config import Settings
from app.core.db import create_engine


async def test_the_pool_takes_its_size_from_settings() -> None:
    engine = create_engine(Settings(db_pool_size=12, db_max_overflow=3))
    try:
        assert engine.pool.size() == 12  # ty: ignore[unresolved-attribute]
        assert engine.pool._max_overflow == 3  # ty: ignore[unresolved-attribute]
    finally:
        await engine.dispose()


def test_the_defaults_leave_room_for_the_turn_lock() -> None:
    settings = Settings()
    assert settings.db_pool_size + settings.db_max_overflow >= 20
