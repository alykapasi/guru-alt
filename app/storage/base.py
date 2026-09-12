"""The object-storage seam: a minimal blob interface for raw uploaded files.

Application code stores and fetches blobs by **key** through :class:`BlobStore`; the backend
(S3-compatible in dev/prod via MinIO/S3/R2, in-memory in tests) is a config swap. Small blobs
(URL pages, OCR images) use the bytes methods (``put``/``get``); large media use the **streaming**
methods (``upload``/``download``) that move data via a local file so neither side holds the whole
blob in memory.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_CONTENT_TYPE = "application/octet-stream"


class BlobNotFound(KeyError):
    """No blob exists at the requested key."""


@runtime_checkable
class BlobStore(Protocol):
    """Store/fetch blobs by key. Keys are caller-chosen (see the project key scheme)."""

    async def put(self, key: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        """Store ``data`` at ``key``; return a backend URI. Overwrites an existing key."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the bytes at ``key`` or raise :class:`BlobNotFound`."""
        ...

    async def upload(self, key: str, src: Path, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        """Stream a local file to ``key`` (bounded memory); return a backend URI."""
        ...

    async def download(self, key: str, dest: Path) -> None:
        """Stream the blob at ``key`` to a local file (bounded memory) or raise BlobNotFound."""
        ...

    async def delete(self, key: str) -> None:
        """Remove ``key`` (idempotent — missing keys are not an error)."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether anything is stored at ``key``.

        A metadata lookup, not a read: the caller asking this (S60's integrity check) walks
        every referenced key, and answering with ``get`` would download the corpus to learn
        that it is still there.
        """
        ...

    async def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        """A time-limited URL to fetch ``key`` directly (for citations / client download)."""
        ...
