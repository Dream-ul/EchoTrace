"""Dependency checks used by the HTTP readiness contract."""

from __future__ import annotations

import os
from typing import Any

from echotrace.infrastructure.object_store import ObjectStoreError, S3CompatibleObjectStore
from echotrace.infrastructure.object_store_config import ObjectStoreConfig
from echotrace.infrastructure.coordination.redis.config import RedisConfig
from echotrace.infrastructure.coordination.redis.client import RedisStreamBus


def dependency_readiness() -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    checks["configuration"] = _configuration_check()
    if checks["configuration"]["status"] != "ok":
        return {"status": "not_ready", "checks": checks}
    checks["mysql"] = _mysql_check()
    checks["redis"] = _redis_check()
    checks["object_store"] = _object_store_check()
    ready = all(item.get("status") == "ok" for item in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}


def _configuration_check() -> dict[str, Any]:
    required = ("ECHOTRACE_LLM_BASE_URL", "ECHOTRACE_LLM_API_KEY", "ECHOTRACE_LLM_MODEL", "ECHOTRACE_DATABASE_URL")
    missing = [key for key in required if not os.getenv(key, "").strip()]
    try:
        ObjectStoreConfig.from_env()
    except RuntimeError as exc:
        missing.append("S3_OBJECT_STORE")
        return {"status": "error", "missing": missing, "detail": str(exc)}
    try:
        RedisConfig.from_env()
    except RuntimeError as exc:
        missing.append("ECHOTRACE_REDIS_URL")
        return {"status": "error", "missing": missing, "detail": str(exc)}
    return {"status": "ok" if not missing else "error", "missing": missing}


def _mysql_check() -> dict[str, Any]:
    try:
        from sqlalchemy import text
        from echotrace.infrastructure.persistence.mysql.database import create_engine
        engine = create_engine(os.environ["ECHOTRACE_DATABASE_URL"], pool_pre_ping=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": _safe_detail(exc)}


def _redis_check() -> dict[str, Any]:
    try:
        import redis
        config = RedisConfig.from_env()
        client = redis.Redis.from_url(config.url, decode_responses=True, socket_timeout=config.socket_timeout_seconds, socket_connect_timeout=config.socket_timeout_seconds)
        if client.ping() is not True:
            raise RuntimeError("Redis PING did not return PONG")
        bus = RedisStreamBus(client)
        roles = ("orchestrator", "researcher", "verifier", "report_composer")
        for role in roles:
            bus.ensure_group(role)
        if int(client.eval("return 1", 0)) != 1:
            raise RuntimeError("Redis scripting is unavailable")
        return {"status": "ok", "url": config.redacted_url(), "v2_roles": list(roles), "scripts": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": _safe_detail(exc)}


def _object_store_check() -> dict[str, Any]:
    try:
        config = ObjectStoreConfig.from_env()
        store = S3CompatibleObjectStore(
            endpoint_url=config.endpoint_url,
            bucket=config.bucket,
            access_key=config.access_key_id,
            secret_key=config.secret_access_key,
            region=config.region,
            max_bytes=config.max_object_bytes,
            use_tls=config.use_tls,
        )
        return store.check_bucket_access()
    except (ObjectStoreError, RuntimeError) as exc:
        return {"status": "error", "detail": _safe_detail(exc)}


def _safe_detail(exc: Exception) -> str:
    return " ".join(str(exc).split())[:240] or exc.__class__.__name__


__all__ = ["dependency_readiness"]
