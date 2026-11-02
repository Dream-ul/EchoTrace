"""Explicit Redis configuration without opening a connection at import time."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, urlparse, urlunparse


@dataclass(frozen=True, slots=True)
class RedisConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    password: str | None = None
    database: int = 0
    socket_timeout_seconds: float = 5.0
    connection_url: str | None = None
    tls: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RedisConfig":
        env = os.environ if environ is None else environ
        raw_url = str(env.get("ECHOTRACE_REDIS_URL", "")).strip()
        raw_host = str(env.get("ECHOTRACE_REDIS_HOST", "")).strip()
        if not raw_url and not raw_host:
            raise RuntimeError("ECHOTRACE_REDIS_URL or ECHOTRACE_REDIS_HOST is required")
        tls_setting = str(env.get("ECHOTRACE_REDIS_TLS", "")).strip().lower()
        tls = tls_setting in {"1", "true", "yes", "on"}
        if raw_url:
            parsed = urlparse(raw_url)
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
                raise RuntimeError("ECHOTRACE_REDIS_URL must be a redis(s) URL")
            if tls_setting and tls != (parsed.scheme == "rediss"):
                raise RuntimeError("ECHOTRACE_REDIS_TLS must agree with the ECHOTRACE_REDIS_URL scheme")
            tls = parsed.scheme == "rediss"
            database = int((parsed.path or "/0").strip("/") or 0)
            return cls(
                host=parsed.hostname,
                port=parsed.port or (6380 if tls else 6379),
                password=parsed.password,
                database=database,
                socket_timeout_seconds=float(env.get("ECHOTRACE_REDIS_TIMEOUT", "5")),
                connection_url=raw_url,
                tls=tls,
            )
        return cls(
            host=raw_host,
            port=int(env.get("ECHOTRACE_REDIS_PORT", "6379")),
            password=env.get("ECHOTRACE_REDIS_PASSWORD") or None,
            database=int(env.get("ECHOTRACE_REDIS_DB", "0")),
            socket_timeout_seconds=float(env.get("ECHOTRACE_REDIS_TIMEOUT", "5")),
            tls=tls,
        )

    @property
    def url(self) -> str:
        if self.connection_url:
            return self.connection_url
        auth = f":{quote(self.password, safe='')}@" if self.password else ""
        scheme = "rediss" if self.tls else "redis"
        return f"{scheme}://{auth}{self.host}:{self.port}/{self.database}"

    def redacted_url(self) -> str:
        parsed = urlparse(self.url)
        auth = "***@" if parsed.password else ""
        host = parsed.hostname or self.host
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or f"/{self.database}"
        return urlunparse((parsed.scheme, f"{auth}{host}{port}", path, "", "", ""))


__all__ = ["RedisConfig"]
