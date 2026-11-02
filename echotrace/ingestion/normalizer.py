"""Normalize public-source records before they enter an investigation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from echotrace.domain.models import Document


class DocumentNormalizer:
    """Convert adapter payloads into documents and remove exact duplicates."""

    def normalize(self, payload: Mapping[str, Any]) -> Document:
        published_at = payload.get("published_at")
        if isinstance(published_at, str):
            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return Document(
            source=str(payload.get("source", "unknown")).strip() or "unknown",
            title=str(payload.get("title", "untitled")).strip() or "untitled",
            content=str(payload.get("content", "")).strip(),
            url=payload.get("url"),
            author=payload.get("author"),
            published_at=published_at,
            metadata=dict(payload.get("metadata") or {}),
        )

    def normalize_many(self, payloads: Iterable[Mapping[str, Any]]) -> list[Document]:
        documents: list[Document] = []
        seen_hashes: set[str] = set()
        for payload in payloads:
            document = self.normalize(payload)
            if document.content_hash in seen_hashes:
                continue
            seen_hashes.add(document.content_hash)
            documents.append(document)
        return documents
