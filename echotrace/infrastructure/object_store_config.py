"""Required S3-compatible object-store configuration for application runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class ObjectStoreConfig:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "us-east-1"
    use_tls: bool = False
    max_object_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ObjectStoreConfig":
        env = os.environ if environ is None else environ
        required = {
            "S3_ENDPOINT_URL": str(env.get("S3_ENDPOINT_URL", "")).strip(),
            "S3_BUCKET": str(env.get("S3_BUCKET", "")).strip(),
            "S3_ACCESS_KEY_ID": str(env.get("S3_ACCESS_KEY_ID", "")).strip(),
            "S3_SECRET_ACCESS_KEY": str(env.get("S3_SECRET_ACCESS_KEY", "")).strip(),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeError("required object-store configuration is missing: " + ", ".join(missing))

        endpoint = required["S3_ENDPOINT_URL"].rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("S3_ENDPOINT_URL must be an absolute http(s) URL")
        use_tls = str(env.get("S3_USE_TLS", "1" if parsed.scheme == "https" else "0")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        if use_tls != (parsed.scheme == "https"):
            raise RuntimeError("S3_USE_TLS must agree with the S3_ENDPOINT_URL scheme")
        try:
            max_object_bytes = int(env.get("S3_MAX_OBJECT_BYTES", str(50 * 1024 * 1024)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("S3_MAX_OBJECT_BYTES must be a positive integer") from exc
        if max_object_bytes < 1:
            raise RuntimeError("S3_MAX_OBJECT_BYTES must be a positive integer")
        return cls(
            endpoint_url=endpoint,
            bucket=required["S3_BUCKET"],
            access_key_id=required["S3_ACCESS_KEY_ID"],
            secret_access_key=required["S3_SECRET_ACCESS_KEY"],
            region=str(env.get("S3_REGION", "us-east-1")).strip() or "us-east-1",
            use_tls=use_tls,
            max_object_bytes=max_object_bytes,
        )


__all__ = ["ObjectStoreConfig"]
