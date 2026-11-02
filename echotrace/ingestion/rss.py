"""RSS/Atom source adapter for public event investigation inputs."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.request import Request, urlopen
from xml.etree import ElementTree


class RssSourceAdapter:
    """Fetch and parse RSS/Atom feeds into normalizer-compatible payloads."""

    def __init__(self, source: str = "rss", timeout: float = 10.0) -> None:
        self.source = source
        self.timeout = timeout

    def fetch(self, feed_url: str, limit: int | None = None) -> list[dict[str, Any]]:
        request = Request(feed_url, headers={"User-Agent": "EchoTrace/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read()
        return self.parse(body, feed_url=feed_url, limit=limit)

    def fetch_documents(self, feed_url: str, limit: int | None = None):
        from .normalizer import DocumentNormalizer

        return DocumentNormalizer().normalize_many(self.fetch(feed_url, limit=limit))

    def parse(self, xml: bytes | str, feed_url: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        root = ElementTree.fromstring(xml)
        entries = list(_rss_entries(root)) or list(_atom_entries(root))
        if limit is not None:
            entries = entries[: max(0, limit)]
        return [self._payload(entry, feed_url) for entry in entries]

    def _payload(self, entry: dict[str, str | None], feed_url: str | None) -> dict[str, Any]:
        content = entry["content"] or entry["summary"] or entry["title"] or ""
        return {
            "source": self.source,
            "title": entry["title"] or "untitled",
            "content": content,
            "url": entry["url"] or feed_url,
            "published_at": _parse_datetime(entry["published_at"]),
            "author": entry["author"],
            "metadata": {"feed_url": feed_url} if feed_url else {},
        }


def _rss_entries(root: ElementTree.Element) -> Iterable[dict[str, str | None]]:
    for item in root.findall(".//item"):
        yield {
            "title": _child_text(item, "title"),
            "url": _child_text(item, "link"),
            "content": _child_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded"),
            "summary": _child_text(item, "description"),
            "published_at": _child_text(item, "pubDate") or _child_text(item, "{http://purl.org/dc/elements/1.1/}date"),
            "author": _child_text(item, "author") or _child_text(item, "{http://purl.org/dc/elements/1.1/}creator"),
        }


def _atom_entries(root: ElementTree.Element) -> Iterable[dict[str, str | None]]:
    namespace = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{namespace}entry"):
        link = next((item.get("href") for item in entry.findall(f"{namespace}link") if item.get("rel", "alternate") == "alternate"), None)
        yield {
            "title": _child_text(entry, f"{namespace}title"),
            "url": link,
            "content": _child_text(entry, f"{namespace}content"),
            "summary": _child_text(entry, f"{namespace}summary"),
            "published_at": _child_text(entry, f"{namespace}published") or _child_text(entry, f"{namespace}updated"),
            "author": _child_text(entry, f"{namespace}author/{namespace}name"),
        }


def _child_text(parent: ElementTree.Element, path: str) -> str | None:
    child = parent.find(path)
    if child is None or child.text is None:
        return None
    value = " ".join(child.text.split())
    return value or None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
