"""What must be true before this process is allowed to serve production traffic (S60).

Every check here guards a setting whose *default is fine in dev and wrong in production*, which
is the class of failure that cannot be caught by a test running with dev defaults: the
credentials of the local MinIO, a database on localhost, a wide-open CORS policy, debug output.
Each one is a value somebody has to remember to override, and forgetting is silent — the service
starts, serves, and is misconfigured.

So they are checked at startup and the process refuses to come up, rather than passing a
readiness probe and taking traffic.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.config import AppEnv, Settings

# The docker-compose credentials. Present in production means nobody set the real ones.
_DEV_BLOB_KEYS = {"minioadmin"}
_DEV_DB_CREDENTIALS = ("guru:guru@",)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


class MisconfiguredForProduction(RuntimeError):
    """Raised at startup when production settings still carry development defaults."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"refusing to start in prod:\n  - {joined}")


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def production_problems(settings: Settings) -> list[str]:
    """Every production misconfiguration in ``settings``, as operator-readable sentences.

    Returns all of them rather than the first: an operator restarting a deployment once per
    discovered problem learns the list one outage at a time.
    """
    problems: list[str] = []

    if settings.debug:
        problems.append("GURU_DEBUG is on — it exposes internals in error responses")

    if any(marker in settings.database_url for marker in _DEV_DB_CREDENTIALS):
        problems.append("GURU_DATABASE_URL still uses the docker-compose credentials")
    if _host_of(settings.database_url) in _LOCAL_HOSTS:
        problems.append("GURU_DATABASE_URL points at localhost")

    if settings.blob_access_key in _DEV_BLOB_KEYS or settings.blob_secret_key in _DEV_BLOB_KEYS:
        problems.append("GURU_BLOB_ACCESS_KEY/SECRET_KEY are the MinIO defaults")
    if _host_of(settings.blob_endpoint_url) in _LOCAL_HOSTS:
        problems.append("GURU_BLOB_ENDPOINT_URL points at localhost")

    if _host_of(settings.redis_url) in _LOCAL_HOSTS:
        problems.append("GURU_REDIS_URL points at localhost — jobs would queue to nothing")

    if "*" in settings.cors_origins:
        problems.append("GURU_CORS_ORIGINS allows every origin")
    if any(_host_of(origin) in _LOCAL_HOSTS for origin in settings.cors_origins):
        problems.append("GURU_CORS_ORIGINS still lists a localhost dev server")

    # Not a credential check — a routing one. Every role has to resolve to a provider that can
    # actually be called, and the two hosted providers are the only ones that take a key.
    if not settings.openrouter_api_key and not settings.anthropic_api_key:
        problems.append("no model provider key is set (GURU_OPENROUTER_API_KEY/ANTHROPIC_API_KEY)")

    # Auth (S21). The development sign-in seam issues a session for the dev learner with no
    # credential at all, so leaving it on in production is not a weak password — it is no
    # password, for anybody who finds the endpoint.
    if settings.dev_auto_login:
        problems.append(
            "GURU_DEV_AUTO_LOGIN is on — /auth/dev-login issues a session with no credential"
        )
    # A session cookie without Secure is sent over plain HTTP, where anything on the path can
    # read it and replay it. The dev default is off because the dev server has no TLS.
    if not settings.session_cookie_secure:
        problems.append("GURU_SESSION_COOKIE_SECURE is off — the session cookie would cross HTTP")
    # SameSite=None removes the browser's own cross-site protection, so it is only ever correct
    # alongside Secure and a deliberate cross-site deployment.
    if settings.session_cookie_samesite == "none" and not settings.session_cookie_secure:
        problems.append("GURU_SESSION_COOKIE_SAMESITE=none requires GURU_SESSION_COOKIE_SECURE")

    return problems


def enforce_production_settings(settings: Settings) -> None:
    """Refuse to start when running as prod with development defaults still in place."""
    if settings.env is not AppEnv.PROD:
        return
    problems = production_problems(settings)
    if problems:
        raise MisconfiguredForProduction(problems)
