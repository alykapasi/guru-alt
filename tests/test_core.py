"""Tests for Phase 0 foundations: settings and request-id middleware."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppEnv, Settings
from app.core.middleware import REQUEST_ID_HEADER
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_runtime_typecheck_off_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars override defaults; prod disables runtime type-checking."""
    monkeypatch.setenv("GURU_ENV", "prod")
    monkeypatch.setenv("GURU_LOG_JSON", "true")
    settings = Settings()
    assert settings.env is AppEnv.PROD
    assert settings.log_json is True
    assert settings.runtime_typecheck is False


@pytest.mark.parametrize("env", ["dev", "test"])
def test_runtime_typecheck_on_outside_prod(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("GURU_ENV", env)
    assert Settings().runtime_typecheck is True


def test_request_id_generated_when_absent(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers.get(REQUEST_ID_HEADER)


def test_request_id_echoed_when_provided(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "abc-123"
