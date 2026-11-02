"""Deterministic normalization provider for already captured source records."""

from __future__ import annotations

from echotrace.domain.models import beijing_now
from hashlib import sha256
from typing import Any
from uuid import uuid4


def run(payload: dict[str, Any]) -> dict[str, Any]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be an array")
    documents = [_normalize(item) for item in records]
    return {"documents": documents, "count": len(documents)}


def _normalize(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("each source record must be an object")
    source = str(record.get("source", "unknown")).strip() or "unknown"
    title = str(record.get("title", "untitled")).strip() or "untitled"
    content = str(record.get("content", "")).strip()
    if not content:
        raise ValueError("source record content cannot be empty")
    content_hash = sha256(content.encode("utf-8")).hexdigest()
    return {
        "document_id": str(record.get("document_id") or f"doc_{uuid4().hex[:12]}"),
        "source": source,
        "title": title,
        "content": content,
        "url": _optional(record.get("url")),
        "published_at": _optional(record.get("published_at")),
        "fetched_at": _optional(record.get("fetched_at")) or beijing_now().isoformat(),
        "author": _optional(record.get("author")),
        "content_hash": content_hash,
        "metadata": dict(record.get("metadata") or {}),
    }


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None
