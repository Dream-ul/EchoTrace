"""Bocha AI Search adapter with tolerant response normalization."""

from __future__ import annotations

from echotrace.domain.models import beijing_now
import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


def _items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("webPages", "results", "items"):
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("value") or value.get("items")
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _document(item: dict[str, Any]) -> dict[str, Any] | None:
    url = str(item.get("url") or item.get("link") or item.get("targetUrl") or "").strip()
    if not url:
        return None
    content = str(item.get("summary") or item.get("snippet") or item.get("content") or "").strip()
    if not content:
        return None
    return {
        "document_id": "bocha_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12],
        "source": "bocha",
        "title": str(item.get("name") or item.get("title") or urlparse(url).netloc),
        "content": content,
        "url": url,
        "published_at": item.get("datePublished") or item.get("published_at"),
        "fetched_at": beijing_now().isoformat(),
        "author": None,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "metadata": {"provider": "bocha"},
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(payload.get("limit") or 10), 20))
    body: dict[str, Any] = {"query": query, "count": limit, "summary": True}
    domains = [str(item).strip() for item in (payload.get("domains") or []) if str(item).strip()]
    if domains:
        body["includeDomains"] = domains
    endpoint = os.environ["BOCHA_BASE_URL"]
    # Bocha's current endpoint is /v1/web-search. Older local configurations
    # used /v1/ai-search; normalize that alias so a deployment does not fail
    # with a misleading 403 while the key itself is valid.
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.path.rstrip("/").endswith("/ai-search"):
        endpoint = urlunparse(parsed_endpoint._replace(path=parsed_endpoint.path.rsplit("/", 1)[0] + "/web-search"))
    request = Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['BOCHA_WEB_SEARCH_API_KEY']}"},
        method="POST",
    )
    with urlopen(request, timeout=28) as response:
        result = json.loads(response.read().decode("utf-8"))
    documents = [_document(item) for item in _items(result)]
    documents = [item for item in documents if item is not None]
    return {"documents": documents[:limit], "count": min(len(documents), limit)}
