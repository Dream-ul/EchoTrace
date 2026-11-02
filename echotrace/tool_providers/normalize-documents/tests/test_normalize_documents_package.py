from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _handler():
    path = Path(__file__).parents[1] / "handler.py"
    spec = spec_from_file_location("normalize_documents_provider", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_documents_preserves_provenance_and_hashes_content():
    result = _handler().run({"records": [{"source": "official", "title": "Notice", "content": "A claim."}]})

    assert result["count"] == 1
    document = result["documents"][0]
    assert document["source"] == "official"
    assert len(document["content_hash"]) == 64
    assert document["fetched_at"]


def test_normalize_documents_rejects_empty_content():
    try:
        _handler().run({"records": [{"source": "x", "content": "  "}]})
    except ValueError as exc:
        assert "content" in str(exc)
    else:
        raise AssertionError("empty content must be rejected")
