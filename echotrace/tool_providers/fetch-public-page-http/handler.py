"""Bounded public page fetcher for the fetch_public_page capability."""

from __future__ import annotations

from echotrace.domain.models import beijing_now
from hashlib import sha256
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)


def _declared_charset(raw: bytes) -> str | None:
    """Read an HTML charset declaration without decoding the whole page first."""
    header = raw[:8192].decode("ascii", errors="ignore")
    match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", header, re.I)
    if match:
        return match.group(1)
    match = re.search(r"charset\s*=\s*([A-Za-z0-9._:-]+)", header, re.I)
    return match.group(1) if match else None


def _decode_page(raw: bytes, charset: str | None) -> tuple[str, str]:
    """Decode HTTP/HTML bytes while avoiding replacement-character ledgers."""
    candidates: list[str] = []
    for value in (charset, _declared_charset(raw), "utf-8", "gb18030", "big5"):
        if value and value.lower() not in {item.lower() for item in candidates}:
            candidates.append(value)
    best_text = ""
    best_score: tuple[int, int] | None = None
    best_charset = candidates[0] if candidates else "utf-8"
    for candidate in candidates:
        try:
            text = raw.decode(candidate, errors="replace")
        except (LookupError, UnicodeError):
            continue
        replacement_count = text.count("\ufffd")
        mojibake_count = sum(text.count(marker) for marker in ("Ã", "Â", "â", "锟", "ï¿½"))
        score = (replacement_count, mojibake_count)
        if best_score is None or score < best_score:
            best_text, best_score, best_charset = text, score, candidate
    return best_text, best_charset


def run(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    request = Request(url, headers={"User-Agent": "EchoTrace/1.0 (+public research)"})
    with urlopen(request, timeout=28) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("page exceeds the 2 MB capture limit")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
    text, selected_charset = _decode_page(raw, charset)
    parser = _Text()
    parser.feed(text if content_type == "text/html" else text[:2_000_000])
    content = "\n".join(parser.parts).strip()
    if not content:
        raise ValueError("page has no readable text")
    digest = sha256(content.encode("utf-8")).hexdigest()
    return {"document": {
        "document_id": "page_" + sha256(url.encode("utf-8")).hexdigest()[:12],
        "source": parsed.netloc,
        "title": parser.parts[0][:200] if parser.parts else parsed.netloc,
        "content": content,
        "url": url,
        "published_at": None,
        "fetched_at": beijing_now().isoformat(),
        "author": None,
        "content_hash": digest,
        "metadata": {
            "provider": "fetch-public-page-http",
            "content_type": content_type,
            "charset": selected_charset,
        },
    }}
