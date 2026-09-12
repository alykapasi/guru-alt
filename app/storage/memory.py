"""In-memory blob store for tests — no network, fully deterministic."""

from pathlib import Path

from app.storage.base import DEFAULT_CONTENT_TYPE, BlobNotFound


class InMemoryBlobStore:
    """A dict-backed :class:`~app.storage.base.BlobStore`."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        self._store[key] = data
        return f"memory://{key}"

    async def get(self, key: str) -> bytes:
        try:
            return self._store[key]
        except KeyError as exc:
            raise BlobNotFound(key) from exc

    async def upload(self, key: str, src: Path, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        self._store[key] = Path(src).read_bytes()
        return f"memory://{key}"

    async def download(self, key: str, dest: Path) -> None:
        try:
            data = self._store[key]
        except KeyError as exc:
            raise BlobNotFound(key) from exc
        Path(dest).write_bytes(data)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        return f"memory://{key}"
