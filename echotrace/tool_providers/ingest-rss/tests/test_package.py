from pathlib import Path

from echotrace.domain.models import Document
from echotrace.ingestion import rss
from echotrace.runtime.tool_gateway import ToolRouter


PACKAGE = Path(__file__).parents[1]


def test_rss_tool_provider_loads_manifest_without_agent_skill_docs():
    router = ToolRouter()
    manifest = router.load_package(PACKAGE)

    assert manifest.name == "ingest-rss"
    assert "ingest_public_feed" in manifest.capabilities
    assert not (PACKAGE / "SKILL.md").exists()


def test_rss_tool_provider_returns_contract_payload(monkeypatch):
    monkeypatch.setattr(
        rss.RssSourceAdapter,
        "fetch_documents",
        lambda self, feed_url, limit=None: [Document(source="rss", title="entry", content="body")],
    )
    router = ToolRouter()
    router.load_package(PACKAGE)

    result = router.call("researcher", "ingest_public_feed", {"feed_url": "https://example.test/feed.xml", "limit": 1})

    assert result["count"] == 1
    assert result["documents"][0]["content"] == "body"
