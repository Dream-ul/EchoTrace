import importlib.util
from email.message import Message
from pathlib import Path
from unittest.mock import patch


def _handler():
    path = Path(__file__).parents[1] / "handler.py"
    spec = importlib.util.spec_from_file_location("http_page_provider_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class _Response:
    headers = Message()
    headers["Content-Type"] = "text/html; charset=utf-8"
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return b"<html><title>Example</title><script>ignore()</script><p>Readable page.</p></html>"


class _GbkResponse:
    headers = Message()
    headers["Content-Type"] = "text/html"
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return "<html><meta charset='gb18030'><p>武汉大学</p></html>".encode("gb18030")


def test_public_page_fetch_strips_non_content_tags_and_hashes_text():
    module = _handler()
    with patch.object(module, "urlopen", return_value=_Response()):
        result = module.run({"url": "https://example.com/page"})
    document = result["document"]
    assert document["content"] == "Example\nReadable page."
    assert "ignore" not in document["content"]
    assert document["metadata"]["provider"] == "fetch-public-page-http"


def test_public_page_fetch_honors_html_gb18030_charset_without_replacement_text():
    module = _handler()
    with patch.object(module, "urlopen", return_value=_GbkResponse()):
        result = module.run({"url": "https://example.com/gbk"})
    document = result["document"]
    assert "武汉大学" in document["content"]
    assert "\ufffd" not in document["content"]
    assert document["metadata"]["charset"].lower() == "gb18030"
