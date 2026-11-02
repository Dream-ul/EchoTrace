from __future__ import annotations

from app.readiness import dependency_readiness


def test_readiness_reports_missing_runtime_dependencies(monkeypatch):
    for key in (
        "ECHOTRACE_LLM_BASE_URL", "ECHOTRACE_LLM_API_KEY", "ECHOTRACE_LLM_MODEL",
        "ECHOTRACE_DATABASE_URL", "ECHOTRACE_REDIS_URL", "ECHOTRACE_REDIS_HOST",
        "S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    result = dependency_readiness()

    assert result["status"] == "not_ready"
    assert result["checks"]["configuration"]["status"] == "error"
    assert "ECHOTRACE_LLM_MODEL" in result["checks"]["configuration"]["missing"]
    assert "S3_OBJECT_STORE" in result["checks"]["configuration"]["missing"]
