"""S3-compatible blob store (MinIO in dev, S3/R2/GCS in prod) via aioboto3.

A fresh client is opened per call: aioboto3 clients are async context managers bound to
the running event loop, so this keeps the store loop-agnostic and safe to share. Fine for
4a's modest upload volume; a pooled client is a later optimization if it matters.
"""

from pathlib import Path
from typing import Any

import aioboto3
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.storage.base import DEFAULT_CONTENT_TYPE, BlobNotFound

_MISSING_CODES = {"NoSuchKey", "404"}


class S3BlobStore:
    """An S3-compatible :class:`~app.storage.base.BlobStore`."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.blob_bucket
        self._session = aioboto3.Session()
        self._kwargs: dict[str, Any] = {
            "service_name": "s3",
            "endpoint_url": settings.blob_endpoint_url,
            "aws_access_key_id": settings.blob_access_key,
            "aws_secret_access_key": settings.blob_secret_key,
            "region_name": settings.blob_region,
        }

    async def put(self, key: str, data: bytes, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        async with self._session.client(**self._kwargs) as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self._bucket}/{key}"

    async def get(self, key: str) -> bytes:
        async with self._session.client(**self._kwargs) as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                    raise BlobNotFound(key) from exc
                raise
            async with resp["Body"] as body:
                data: bytes = await body.read()
                return data

    async def upload(self, key: str, src: Path, *, content_type: str = DEFAULT_CONTENT_TYPE) -> str:
        async with self._session.client(**self._kwargs) as s3:
            with open(src, "rb") as fileobj:
                await s3.upload_fileobj(
                    fileobj, self._bucket, key, ExtraArgs={"ContentType": content_type}
                )
        return f"s3://{self._bucket}/{key}"

    async def download(self, key: str, dest: Path) -> None:
        async with self._session.client(**self._kwargs) as s3:
            try:
                with open(dest, "wb") as fileobj:
                    await s3.download_fileobj(self._bucket, key, fileobj)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                    raise BlobNotFound(key) from exc
                raise

    async def delete(self, key: str) -> None:
        async with self._session.client(**self._kwargs) as s3:
            await s3.delete_object(Bucket=self._bucket, Key=key)

    async def presigned_url(self, key: str, *, expires_in: int = 3600) -> str:
        async with self._session.client(**self._kwargs) as s3:
            url: str = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url
