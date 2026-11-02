import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch


def _handler():
    path = Path(__file__).parents[1] / "handler.py"
    spec = importlib.util.spec_from_file_location("bocha_provider_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class _Response:
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps({"data": {"webPages": {"value": [{"name": "官方", "url": "https://example.com/a", "snippet": "内容"}]}}}).encode()


def test_bocha_normalizes_web_pages_shape():
    module = _handler()
    with patch.dict(os.environ, {"BOCHA_BASE_URL": "https://bocha.test/search", "BOCHA_WEB_SEARCH_API_KEY": "test-key"}), patch.object(module, "urlopen", return_value=_Response()):
        result = module.run({"query": "产品争议", "limit": 2})
    assert result["count"] == 1
    assert result["documents"][0]["source"] == "bocha"
