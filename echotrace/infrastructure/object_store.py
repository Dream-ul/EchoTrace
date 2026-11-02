"""Object-storage boundary for raw source and Trace media artifacts."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ALLOWED_MEDIA_TYPES = frozenset({
    "text/html", "text/plain", "application/json", "application/pdf",
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/webm", "audio/mpeg", "audio/wav",
})
MAX_OBJECT_KEY_LENGTH = 512


class ObjectStoreError(RuntimeError):
    """Raised when an object cannot be stored or retrieved safely."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    content_hash: str
    byte_size: int
    mime_type: str
    etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_key": self.object_key,
            "content_hash": self.content_hash,
            "byte_size": self.byte_size,
            "mime_type": self.mime_type,
            "etag": self.etag,
        }


def _safe_key(object_key: str) -> str:
    value = str(object_key or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or value.startswith(".") or "/../" in f"/{value}/" or value.endswith("/.."):
        raise ObjectStoreError("object key must be a relative, traversal-free path")
    if len(value) > MAX_OBJECT_KEY_LENGTH:
        raise ObjectStoreError(f"object key exceeds max length of {MAX_OBJECT_KEY_LENGTH} characters")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ObjectStoreError("object key contains an unsafe path segment")
    return value


def _mime_type(value: str | None, object_key: str) -> str:
    candidate = str(value or mimetypes.guess_type(object_key)[0] or "application/octet-stream").lower()
    if candidate not in ALLOWED_MEDIA_TYPES:
        raise ObjectStoreError(f"MIME type is not allowed: {candidate}")
    return candidate


class LocalObjectStore:
    """Filesystem-backed test fixture; never use for application runtime."""

    def __init__(self, root: str | Path, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max(1, int(max_bytes))

    def _path(self, object_key: str) -> Path:
        safe = _safe_key(object_key)
        path = (self.root / safe).resolve()
        if self.root not in path.parents:
            raise ObjectStoreError("object key escapes the configured store root")
        return path

    def put_bytes(self, object_key: str, data: bytes, *, mime_type: str | None = None, expected_hash: str | None = None) -> StoredObject:
        payload = bytes(data)
        if len(payload) > self.max_bytes:
            raise ObjectStoreError(f"object exceeds max size of {self.max_bytes} bytes")
        safe = _safe_key(object_key)
        content_type = _mime_type(mime_type, safe)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_hash and expected_hash != digest:
            raise ObjectStoreError("object content hash does not match expected_hash")
        path = self._path(safe)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, path)
        return StoredObject(safe, digest, len(payload), content_type, etag=digest)

    def get_bytes(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise ObjectStoreError(f"object not found: {object_key}")
        return path.read_bytes()

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).is_file()


class S3CompatibleObjectStore:
    """S3-compatible object store used by API and worker runtime."""

    def __init__(self, *, endpoint_url: str, bucket: str, access_key: str, secret_key: str, region: str | None = None, max_bytes: int = 50 * 1024 * 1024, use_tls: bool = False) -> None:
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.max_bytes = max(1, int(max_bytes))
        self.use_tls = bool(use_tls)
        self._client: Any | None = None

    def _s3(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - depends on deployment extras
                raise ObjectStoreError("S3-compatible backend requires boto3 and botocore") from exc
            self._client = boto3.client(
                "s3", endpoint_url=self.endpoint_url, aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key, region_name=self.region,
                verify=self.use_tls,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._client

    def put_bytes(self, object_key: str, data: bytes, *, mime_type: str | None = None, expected_hash: str | None = None) -> StoredObject:
        payload = bytes(data)
        if len(payload) > self.max_bytes:
            raise ObjectStoreError(f"object exceeds max size of {self.max_bytes} bytes")
        safe = _safe_key(object_key)
        content_type = _mime_type(mime_type, safe)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_hash and expected_hash != digest:
            raise ObjectStoreError("object content hash does not match expected_hash")
        client = self._s3()
        existing = self._head_object(client, safe)
        if existing is not None:
            metadata = existing.get("Metadata") or {}
            if metadata.get("sha256") != digest:
                raise ObjectStoreError("object keys are immutable and already contain different content")
            return StoredObject(safe, digest, int(existing.get("ContentLength", len(payload))), content_type, etag=_clean_etag(existing.get("ETag")))
        try:
            response = client.put_object(
                Bucket=self.bucket, Key=safe, Body=payload, ContentType=content_type,
                Metadata={"sha256": digest}, IfNoneMatch="*",
            )
        except Exception as exc:
            if _error_status(exc) == 412:
                existing = self._head_object(client, safe)
                if existing is not None and (existing.get("Metadata") or {}).get("sha256") == digest:
                    return StoredObject(safe, digest, len(payload), content_type, etag=_clean_etag(existing.get("ETag")))
                raise ObjectStoreError("object keys are immutable and already contain different content") from exc
            raise ObjectStoreError("S3-compatible object write failed") from exc
        return StoredObject(safe, digest, len(payload), content_type, etag=_clean_etag(response.get("ETag")))

    def get_bytes(self, object_key: str) -> bytes:
        response = self._s3().get_object(Bucket=self.bucket, Key=_safe_key(object_key))
        body = response["Body"]
        try:
            payload = body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        expected_hash = (response.get("Metadata") or {}).get("sha256")
        if expected_hash and hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ObjectStoreError("stored object content hash does not match its SHA-256 metadata")
        return payload

    def exists(self, object_key: str) -> bool:
        return self._head_object(self._s3(), _safe_key(object_key)) is not None

    def check_bucket_access(self) -> dict[str, Any]:
        """Verify bucket access plus write/read/hash/delete permissions."""

        client = self._s3()
        try:
            client.head_bucket(Bucket=self.bucket)
            key = f"_healthchecks/{uuid.uuid4().hex}.txt"
            payload = f"echotrace-health:{uuid.uuid4().hex}".encode("ascii")
            digest = hashlib.sha256(payload).hexdigest()
            client.put_object(
                Bucket=self.bucket, Key=key, Body=payload, ContentType="text/plain",
                Metadata={"sha256": digest}, IfNoneMatch="*",
            )
            result = client.get_object(Bucket=self.bucket, Key=key)
            body = result["Body"]
            try:
                actual = body.read()
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            if actual != payload or hashlib.sha256(actual).hexdigest() != digest:
                raise ObjectStoreError("object-store health probe failed read-back hash validation")
            if (result.get("Metadata") or {}).get("sha256") != digest:
                raise ObjectStoreError("object-store health probe did not preserve SHA-256 metadata")
            client.delete_object(Bucket=self.bucket, Key=key)
            return {"status": "ok", "bucket": self.bucket, "probe": "write_read_hash_delete"}
        except ObjectStoreError:
            raise
        except Exception as exc:
            raise ObjectStoreError("S3-compatible bucket access check failed") from exc

    def ensure_bucket(self) -> dict[str, str]:
        """Create the configured bucket once when the service account permits it."""

        client = self._s3()
        try:
            client.head_bucket(Bucket=self.bucket)
            return {"status": "ok", "bucket": self.bucket, "created": "false"}
        except Exception as exc:
            if _error_status(exc) not in {400, 403, 404} and _error_code(exc) not in {"404", "NoSuchBucket", "NotFound"}:
                raise ObjectStoreError("S3-compatible bucket lookup failed") from exc
        try:
            client.create_bucket(Bucket=self.bucket)
        except Exception as exc:
            # Another API/worker may have created it between head and create.
            if _error_code(exc) not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise ObjectStoreError("S3-compatible bucket creation failed") from exc
        return {"status": "ok", "bucket": self.bucket, "created": "true"}

    def check_bucket_ready(self) -> dict[str, str]:
        """Check bucket existence without creating or mutating objects."""

        try:
            self._s3().head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise ObjectStoreError("configured S3-compatible bucket is not available") from exc
        return {"status": "ok", "bucket": self.bucket}

    def _head_object(self, client: Any, key: str) -> dict[str, Any] | None:
        try:
            return client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _error_status(exc) == 404 or _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ObjectStoreError("S3-compatible object metadata lookup failed") from exc


def _error_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return int(status) if isinstance(status, int) else None


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    return str(error.get("Code", "")) if isinstance(error, dict) else ""


def _clean_etag(value: Any) -> str | None:
    return str(value).strip('"') if value else None


__all__ = ["ALLOWED_MEDIA_TYPES", "LocalObjectStore", "MAX_OBJECT_KEY_LENGTH", "ObjectStoreError", "S3CompatibleObjectStore", "StoredObject"]
