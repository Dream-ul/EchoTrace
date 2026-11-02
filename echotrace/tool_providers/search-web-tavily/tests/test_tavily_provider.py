import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch


def _handler():
    path = Path(__file__).parents[1] / "handler.py"
    spec = importlib.util.spec_from_file_location("tavily_provider_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class _Response:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps({"results": [{"title": "Official", "url": "https://example.com/a", "content": "A fact", "score": 0.9}]}).encode()


def test_tavily_normalizes_result_and_preserves_url():
    module = _handler()
    with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}), patch.object(module, "urlopen", return_value=_Response()):
        result = module.run({"query": "product issue", "limit": 2})
    assert result["count"] == 1
    assert result["documents"][0]["url"] == "https://example.com/a"
    assert result["documents"][0]["content_hash"]
