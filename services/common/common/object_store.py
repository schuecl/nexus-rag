"""NFR-12: durable storage for the original uploaded file, independent of
the chunk vectors in Qdrant and the metadata row in Postgres -- neither of
those substitutes for retaining the source document itself. Two backends:
local filesystem (dev) and any S3-compatible object store (production --
"the existing enterprise S3 or Ceph RGW or another validated S3-compatible
platform", per REQUIREMENTS.md's hardening backlog). Selected via
OBJECT_STORE_BACKEND; shared by ingestion-api (writes the original at
upload time) and ingestion-worker (reads it back to process).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class FilesystemObjectStore:
    """Dev backend -- a directory on a mounted volume. Not suitable for a
    multi-replica production deployment: ingestion-api and ingestion-worker
    would each need the exact same filesystem mounted, which this doesn't
    coordinate or provide -- see S3ObjectStore for that."""

    def __init__(self, base_path: str):
        self._base = Path(base_path).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are always server-generated (document-id-based -- see
        # upload.py), never derived directly from user input like a
        # filename, so this traversal check is defense in depth rather than
        # a load-bearing guard -- cheap enough to keep regardless.
        path = (self._base / key).resolve()
        if not (path == self._base or self._base in path.parents):
            raise ValueError(f"invalid object key: {key!r}")
        return path

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3ObjectStore:
    """Production backend -- any S3-compatible endpoint. Uses boto3's
    generic S3 client pointed at a configurable endpoint_url rather than
    assuming AWS, so an existing enterprise S3 or Ceph RGW deployment works
    the same way a real AWS bucket would."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ):
        import boto3  # local import: only needed when this backend is selected

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put(self, key: str, content: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=content)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    backend = os.environ.get("OBJECT_STORE_BACKEND", "filesystem")
    if backend == "filesystem":
        return FilesystemObjectStore(os.environ.get("OBJECT_STORE_PATH", "/srv/object-store"))
    if backend == "s3":
        return S3ObjectStore(
            endpoint_url=os.environ["OBJECT_STORE_S3_ENDPOINT"],
            bucket=os.environ["OBJECT_STORE_S3_BUCKET"],
            access_key=os.environ["OBJECT_STORE_S3_ACCESS_KEY"],
            secret_key=os.environ["OBJECT_STORE_S3_SECRET_KEY"],
            region=os.environ.get("OBJECT_STORE_S3_REGION", "us-east-1"),
        )
    raise ValueError(f"unknown OBJECT_STORE_BACKEND: {backend!r}")


def document_object_key(document_id) -> str:
    """One canonical key format, shared by every writer/reader (upload.py,
    the future ingestion-worker) so this isn't reimplemented per caller."""
    return f"documents/{document_id}/original"
