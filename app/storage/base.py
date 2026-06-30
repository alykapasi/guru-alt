"""The object-storage seam: a minimal blob interface for raw uploaded files.

Application code stores and fetches raw bytes by **key** through :class:`BlobStore`; the
backend (S3-compatible in dev/prod via MinIO/S3/R2, in-memory in tests) is a config swap.
4a uploads are small and app-proxied (``put`` takes bytes); streaming + presigned direct
upload arrive with 4b's large media.
"""

from typing import Protocol, runtime_checkable

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class BlobNotFound(KeyError):
    """No blob exists at the requested key."""


@runtime_checkable
class BlobStore(Protocol):
    """Store/fetch raw bytes by key. Keys are caller-chosen (see the project key scheme)."""

    async def put(self, key: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        """Store ``data`` at ``key``; return a backend URI. Overwrites an existing key."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the bytes at ``key`` or raise :class:`BlobNotFound`."""
        ...

    async def delete(self, key: str) -> None:
        """Remove ``key`` (idempotent — missing keys are not an error)."""
        ...

    async def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        """A time-limited URL to fetch ``key`` directly (for citations / client download)."""
        ...
