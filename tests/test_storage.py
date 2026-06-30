"""Object-storage seam + taskiq broker seam (unit; S3 path skipped without MinIO)."""

import uuid

import httpx
import pytest

from app.core.config import AppEnv, Settings, get_settings
from app.storage import BlobNotFound, InMemoryBlobStore, S3BlobStore
from app.workers.broker import build_broker

_ENDPOINT = get_settings().blob_endpoint_url


def _minio_up() -> bool:
    try:
        return httpx.get(f"{_ENDPOINT}/minio/health/live", timeout=2.0).status_code == 200
    except Exception:
        return False


_MINIO = _minio_up()


# --- in-memory store --------------------------------------------------------


async def test_in_memory_put_get_delete() -> None:
    store = InMemoryBlobStore()
    key = "learner/source/abc"
    uri = await store.put(key, b"hello world", content_type="text/plain")
    assert uri == "memory://learner/source/abc"
    assert await store.get(key) == b"hello world"
    await store.delete(key)
    with pytest.raises(BlobNotFound):
        await store.get(key)


async def test_in_memory_overwrites_and_missing_delete_is_noop() -> None:
    store = InMemoryBlobStore()
    await store.put("k", b"v1")
    await store.put("k", b"v2")
    assert await store.get("k") == b"v2"
    await store.delete("missing")  # no raise


# --- broker seam ------------------------------------------------------------


def test_broker_in_memory_for_test_env() -> None:
    broker = build_broker(Settings(env=AppEnv.TEST))
    assert type(broker).__name__ == "InMemoryBroker"


def test_broker_redis_for_dev_env() -> None:
    broker = build_broker(Settings(env=AppEnv.DEV, redis_url="redis://localhost:6379/0"))
    assert type(broker).__name__ == "ListQueueBroker"


# --- S3-compatible store (MinIO) --------------------------------------------


@pytest.mark.skipif(not _MINIO, reason="MinIO not running")
async def test_s3_blob_store_roundtrip() -> None:
    store = S3BlobStore(get_settings())
    key = f"test/{uuid.uuid4()}"
    uri = await store.put(key, b"hello minio", content_type="text/plain")
    assert uri.endswith(key)
    assert await store.get(key) == b"hello minio"
    assert key in await store.presigned_url(key)
    await store.delete(key)
    with pytest.raises(BlobNotFound):
        await store.get(key)
