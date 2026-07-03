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

    # Object storage (S3-compatible) — raw uploaded files. Dev defaults target the
    # MinIO docker-compose service; prod overrides via GURU_BLOB_* env vars.
    blob_endpoint_url: str = "http://localhost:9000"
    blob_bucket: str = "guru-uploads"
    blob_access_key: str = "minioadmin"
    blob_secret_key: str = "minioadmin"
    blob_region: str = "us-east-1"

    # Ingestion: cap uploads (streamed to disk, so this bounds disk not RAM) and choose where
    # the pipeline spools blobs. Default cap 1 GiB; None tmp dir = the system default.
    max_upload_bytes: int = 1_073_741_824
    ingest_tmp_dir: str | None = None

    # Ingestion concurrency/batching (Phase A large-doc speed). Scanned-PDF pages are OCR'd
    # with at most ``ocr_concurrency`` vision calls in flight; chunk embeddings are sent in
    # batches of ``embed_batch_size`` with at most ``embed_concurrency`` batches in flight.
    # DB writes stay serialized regardless — only the network/CPU work is parallel.
    ocr_concurrency: int = 5
    embed_batch_size: int = 128
    embed_concurrency: int = 4

    # Audio/video ASR (Phase 4b). Transcription runs on faster-whisper (optional dep — install
    # the ``asr`` extra); these pick the model size + runtime. Defaults are CPU-friendly.
    asr_model: str = "base"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"

    # Video demux (Phase 4b). The ffmpeg/ffprobe binaries (system tools, not a Python dep) split
    # a video into its audio track (→ ASR) and up to ``video_max_keyframes`` evenly-spaced frames
    # (→ vision-OCR). Point the *_bin settings at non-default paths if not on PATH.
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    video_max_keyframes: int = 20

    # Logging
    log_level: str = "INFO"
    log_json: bool = False  # False = human-friendly console; set True in prod.

    # LLM — code references *roles*; each role maps to "provider:model" per env.
    # Providers: ollama (local), openrouter (cloud), anthropic. Dev defaults to
    # Ollama so chat works offline; prod overrides via GURU_MODEL_* env vars.
    model_fast: str = "ollama:llama3.2"
    model_smart: str = "ollama:llama3.2"
    model_genius: str = "ollama:llama3.2"
    # VISION must resolve to a *multimodal* model (image input). Dev: an Ollama vision
    # model (pull `llama3.2-vision`); prod maps to a multimodal Claude.
    model_vision: str = "ollama:llama3.2-vision"
    model_embed: str = "ollama:nomic-embed-text"

    # Embedding vector dimension — must match the EMBED model's output (nomic = 768) and
    # the pgvector column. Changing the EMBED model's dim is a schema migration.
    embed_dim: int = 768

    ollama_base_url: str = "http://localhost:11434/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""

    # Default cap on assistant output tokens for a chat turn.
    chat_max_tokens: int = 2048

    @property
    def runtime_typecheck(self) -> bool:
        """Whether beartype runtime checks should be active (dev/test only)."""
        return self.env in (AppEnv.DEV, AppEnv.TEST)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
