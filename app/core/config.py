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

    # Ingestion job control (S37). ``max_upload_bytes`` bounds the bytes that arrive; it bounds
    # nothing about what they *expand into* — a 40 MB PDF can be tens of millions of characters
    # and thousands of embed calls. These cap the work itself, per job.
    #
    # A claim's lease is derived as ``ingest_job_timeout_seconds + ingest_lease_grace_seconds``
    # rather than configured separately, so the lease *strictly dominates* the job's own
    # deadline: a job still running cannot have an expired lease, and an expired lease
    # therefore means the worker died. That removes any need to renew a lease mid-job —
    # renewal would have to commit, and the only transaction available to commit is the one
    # holding the job's half-written chunks. The grace covers the gap between the timeout
    # firing and the FAILED status landing.
    #
    # ``ingest_max_attempts`` stops a source that kills its worker every time from cycling
    # forever. ``ingest_max_concurrent_jobs`` is a *soft* cap — see claim_source.
    ingest_job_timeout_seconds: int = 3600
    ingest_lease_grace_seconds: int = 120
    ingest_max_attempts: int = 3
    ingest_max_concurrent_jobs: int = 4
    ingest_max_extracted_chars: int = 20_000_000
    ingest_max_chunks: int = 5_000

    # Reconciliation (S36). A source row is committed before its job is enqueued, so a queue
    # outage between the two leaves a source nobody will ever process. The sweep re-enqueues
    # anything stranded that long — PENDING with nothing happening to it, or PROCESSING with a
    # lapsed lease — and parks what has exhausted its attempts as FAILED, so an abandoned
    # source is visible rather than sitting in PENDING where nothing would look at it again.
    # The grace must exceed normal queue latency, or the sweep re-enqueues jobs merely waiting
    # their turn. Interval 0 disables the worker's background sweep.
    ingest_reconcile_grace_seconds: int = 300
    ingest_reconcile_interval_seconds: int = 120
    ingest_reconcile_batch: int = 100

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

    # Per-chunk KC auto-tagging (Phase 4b). After chunking, the FAST model tags each chunk with the
    # KCs it teaches, scoped to the source's subject/topic. Tags below the confidence floor are
    # dropped; at most ``kc_tag_concurrency`` tagging calls run at once (DB writes stay serialized).
    kc_tag_min_confidence: float = 0.5
    kc_tag_concurrency: int = 5

    # Logging
    log_level: str = "INFO"
    log_json: bool = False  # False = human-friendly console; set True in prod.

    # CORS (Phase 7): origins allowed to call the API from a browser. Dev default is the Vite
    # dev server; prod overrides via GURU_CORS_ORIGINS (JSON array, e.g. '["https://app.example"]').
    cors_origins: list[str] = ["http://localhost:5173"]

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

    # Transport limits applied to every provider SDK client. The 60s ceiling is per network
    # read, not per turn, so a long streamed answer is unaffected — it bounds a provider that
    # has stopped responding. Retries are the SDK's own (connection errors and 429/5xx only).
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    ollama_base_url: str = "http://localhost:11434/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""

    # Default cap on assistant output tokens for a chat turn.
    chat_max_tokens: int = 2048

    # Largest message a learner may send. Rejected by the schema, before anything is paid for.
    # Generous enough to paste a long question or a code sample; short of "paste a book".
    chat_max_input_chars: int = 20_000

    # How many prior messages a turn carries into the prompt. Every turn used to forward the
    # *entire* conversation, so a long-running conversation's cost and context grew without
    # bound until the provider refused it. Durable facts survive truncation through
    # app/memory/, which is the product's existing answer to long-conversation continuity.
    chat_history_max_messages: int = 40

    # Rolling 24h per-learner ceilings, checked before a turn starts. Both are enforced
    # because neither covers the other: cost is unknown for a model with no price entry (see
    # app/llm/pricing.py), and tokens say nothing about how expensive a model is. Either
    # exceeded refuses the turn. 0 disables that ceiling.
    learner_daily_cost_usd_limit: float = 5.0
    learner_daily_token_limit: int = 2_000_000

    # Max negotiation rounds for the refinement gate before it auto-commits the latest proposal.
    refinement_max_rounds: int = 3

    # Number of light-test items a placement run administers (the cost/UX tuning knob).
    placement_light_test_size: int = 3

    # Gap (minutes) beyond which two consecutive learning events are treated as different
    # sessions — shared by every profile estimator that reasons about session-scoped behavior,
    # and by the tracer's repeat-exposure discount (mastery.recent_attempts_at_item): one
    # notion of "the same sitting", not two that can drift apart.
    profile_session_gap_minutes: int = 30

    # Cap on how many KCs a generated lesson plan targets at once — cost/UX bound on a
    # runaway subject graph. Review steps (due retention) are added on top, uncapped.
    lesson_plan_max_steps: int = 20

    # How many of the (soonest-due-first) due reviews GET /reviews/due eagerly resolves an
    # answerable item for — bounds worst-case LLM calls per request; the due list itself is
    # bounded separately (see due_reviews_limit), only item resolution beyond this is skipped.
    reviews_due_item_limit: int = 10

    # Hard cap on the due-reviews query itself (mastery.due_reviews) — deliberately much larger
    # than reviews_due_item_limit; a real (not "unbounded") bound so a learner with a huge
    # backlog can't pull unbounded rows in one request.
    due_reviews_limit: int = 200

    # Memory write-back (Phase 5). How many of a conversation's most recent messages get fed to
    # extraction — cost/UX bound, same idiom as placement_light_test_size. A candidate memory is
    # skipped as a near-duplicate if its cosine distance to an existing same-(learner, kind)
    # memory is at or below this threshold. How many memories a tutor turn retrieves for context.
    memory_extraction_window: int = 20
    memory_dedup_max_distance: float = 0.05
    memory_retrieval_limit: int = 5

    # Max tool-execution rounds per agentic turn (Phase 6) — bounds worst-case LLM calls
    # (agentic_max_iterations + 1) per turn against a runaway tool-calling loop.
    agentic_max_iterations: int = 4

    # Live-fetch tool (Phase 6): cap on how much extracted webpage text fetch_webpage returns
    # to the model — cost/UX bound, same idiom as placement_light_test_size.
    fetch_webpage_max_chars: int = 6_000

    # Max attempt-rounds per guided-practice workflow turn (Phase 6) — same
    # cap-then-degrade-gracefully idiom as refinement_max_rounds / agentic_max_iterations.
    workflow_max_rounds: int = 3

    # Retrieval grounding for chat/workflow turns (Phase 7): smaller than retrieve()'s general
    # default since this runs on every subject-scoped turn and system-prompt length matters
    # more here than in a one-off tool call.
    chat_grounding_limit: int = 5

    # Notes (Phase 8). Caps on what one catch-up distillation reads — cost/UX bounds, same
    # idiom as memory_extraction_window. Messages come from subject-scoped conversations
    # past the note's watermark; outcome events from the topic's KCs.
    note_distill_max_messages: int = 150
    note_distill_max_outcome_events: int = 50

    @property
    def runtime_typecheck(self) -> bool:
        """Whether beartype runtime checks should be active (dev/test only)."""
        return self.env in (AppEnv.DEV, AppEnv.TEST)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
