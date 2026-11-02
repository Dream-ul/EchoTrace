"""Executable handler for the public RSS/Atom ingestion Tool provider."""

from __future__ import annotations

from typing import Any

from echotrace.ingestion import RssSourceAdapter


def run(payload: dict[str, Any]) -> dict[str, Any]:
    feed_url = str(payload.get("feed_url", "")).strip()
    if not feed_url:
        raise ValueError("feed_url is required")
    limit = payload.get("limit")
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be positive")
    documents = RssSourceAdapter(timeout=10.0).fetch_documents(feed_url, limit=int(limit) if limit is not None else None)
    return {"documents": [_document_payload(document) for document in documents], "count": len(documents)}


def _document_payload(document) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "source": document.source,
        "title": document.title,
        "content": document.content,
        "url": document.url,
        "published_at": document.published_at.isoformat() if document.published_at else None,
        "fetched_at": document.fetched_at.isoformat(),
        "author": document.author,
        "content_hash": document.content_hash,
        "metadata": document.metadata,
    }
