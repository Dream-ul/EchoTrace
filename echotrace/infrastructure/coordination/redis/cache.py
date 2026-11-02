"""Short-lived provider response cache; final conclusions are excluded."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .contracts import RedisKeys


class RedisResponseCache:
    def __init__(self, client: Any, *, keys: RedisKeys | None = None, default_ttl_seconds: int = 300, max_value_bytes: int = 512_000) -> None:
        self.client = client
        self.keys = keys or RedisKeys()
        self.default_ttl_seconds = max(1, int(default_ttl_seconds))
        self.max_value_bytes = max(1, int(max_value_bytes))

    def key(self, *, capability: str, provider: str, provider_version: str, schema_version: str, request: dict[str, Any]) -> str:
        request_hash = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return self.keys.cache(capability, provider, provider_version, schema_version, request_hash)

    def get(self, *, capability: str, provider: str, provider_version: str, schema_version: str, request: dict[str, Any]) -> dict[str, Any] | None:
        try:
            value = self.client.get(self.key(capability=capability, provider=provider, provider_version=provider_version, schema_version=schema_version, request=request))
        except Exception:
            return None
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return None
        return dict(value) if isinstance(value, dict) else None

    def set(self, *, capability: str, provider: str, provider_version: str, schema_version: str, request: dict[str, Any], response: dict[str, Any], ttl_seconds: int | None = None) -> str:
        forbidden = {"final_verdict", "assessment", "conclusion", "evidence_ledger"}
        if forbidden.intersection(response):
            raise ValueError("final conclusions and evidence ledgers cannot be cached")
        key = self.key(capability=capability, provider=provider, provider_version=provider_version, schema_version=schema_version, request=request)
        encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded.encode("utf-8")) > self.max_value_bytes:
            raise ValueError("Redis cache value exceeds max_value_bytes")
        try:
            self.client.set(key, encoded, ex=max(1, int(ttl_seconds or self.default_ttl_seconds)))
        except Exception:
            # Cache is an optimization; callers continue with the provider.
            pass
        return key


__all__ = ["RedisResponseCache"]
