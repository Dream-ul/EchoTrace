"""Tavily search adapter; no SDK is required by the provider package."""

from __future__ import annotations

from echotrace.domain.models import beijing_now
import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def _document(item: dict[str, Any]) -> dict[str, Any] | None:
    url = str(item.get("url", "")).strip()
    if not url:
        return None
    content = str(item.get("raw_content") or item.get("content") or "").strip()
    if not content:
        return None
    return {
        "document_id": "tavily_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12],
        "source": "tavily",
        "title": str(item.get("title") or urlparse(url).netloc),
        "content": content,
        "url": url,
        "published_at": item.get("published_date"),
        "fetched_at": beijing_now().isoformat(),
        "author": None,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "metadata": {"provider": "tavily", "score": item.get("score")},
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(payload.get("limit") or 10), 20))
    body: dict[str, Any] = {
        "api_key": os.environ["TAVILY_API_KEY"],
        "query": query,
        "search_depth": "advanced",
        "max_results": limit,
        "include_answer": False,
        "include_raw_content": True,
    }
    domains = [str(item).strip() for item in (payload.get("domains") or []) if str(item).strip()]
    if domains:
        body["include_domains"] = domains
    endpoint = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com/search")
    request = Request(endpoint, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=28) as response:
        result = json.loads(response.read().decode("utf-8"))
    documents = [_document(item) for item in result.get("results", []) if isinstance(item, dict)]
    documents = [item for item in documents if item is not None]
    return {"documents": documents, "count": len(documents)}
