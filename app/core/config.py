"""Typed application settings, loaded from the environment (prefix ``GURU_``).

All configuration flows through :class:`Settings`. Code never reads ``os.environ``
directly (except the beartype-claw guard in ``app.__init__``, which must run before
this module can be imported).
"""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    """Deployment environment. Drives logging style and runtime type-checking."""

    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Settings(BaseSettings):
    """Application settings. Override any field with ``GURU_<FIELD>`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="GURU_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: AppEnv = AppEnv.DEV
    debug: bool = False

    # PostgreSQL (async driver). Host port 5433 — see docker-compose.yml.
    database_url: str = "postgresql+asyncpg://guru:guru@localhost:5433/guru"
    db_echo: bool = False

    # Redis — declared now, used from Phase 4 (background jobs).
    redis_url: str = "redis://localhost:6379/0"

    # Logging
    log_level: str = "INFO"
    log_json: bool = False  # False = human-friendly console; set True in prod.

    @property
    def runtime_typecheck(self) -> bool:
        """Whether beartype runtime checks should be active (dev/test only)."""
        return self.env in (AppEnv.DEV, AppEnv.TEST)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
