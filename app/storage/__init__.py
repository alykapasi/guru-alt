"""Object storage seam: store raw uploads by key, backend swappable by config."""

from app.core.config import Settings
from app.storage.base import DEFAULT_CONTENT_TYPE, BlobNotFound, BlobStore
from app.storage.memory import InMemoryBlobStore
from app.storage.s3 import S3BlobStore

__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "BlobNotFound",
    "BlobStore",
    "InMemoryBlobStore",
    "S3BlobStore",
    "build_blob_store",
]


def build_blob_store(settings: Settings) -> BlobStore:
    """The configured object store (S3-compatible: MinIO in dev, S3/R2/GCS in prod)."""
    return S3BlobStore(settings)
