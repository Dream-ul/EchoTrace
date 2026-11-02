import pytest
from pathlib import Path
from time import sleep
import json

from echotrace.runtime.policies import InvestigationPolicy
from echotrace.runtime.tool_gateway import ToolContractError, ToolProviderManifest, ToolRouter, ToolUnavailable


def _manifest(name="test-search", **kwargs):
    return ToolProviderManifest(
        name=name,
        description=kwargs.pop("description", f"Test Tool provider {name}."),
        capabilities=kwargs.pop("capabilities", ("search_web",)),
        owners=("researcher",),
        input_schema={"query": "string"},
        output_schema={"documents": "array"},
        **kwargs,
    )


def _write_package(root, name, *, handler_source, fallback_providers=()):
    package = root / name
    package.mkdir()
    fallback_line = f"fallback_providers: [{', '.join(fallback_providers)}]\n" if fallback_providers else ""
    (package / "manifest.yaml").write_text(
        "\n".join(
            [
                f"name: {name}",
                "version: 1.0.0",
                f"description: Test Tool provider {name}.",
                "owners: [researcher]",
                "capabilities: [search_web]",
                "exposure: core",
                "exposure_reason: High-frequency test capability.",
                "input:",
                "  query: string",
                "output:",
                "  documents: array",
                "failure_mode: fallback",
                fallback_line.rstrip(),
                "entrypoint: handler.py:run",
            ]
        ).replace("\n\n", "\n"),
        encoding="utf-8",
    )
    (package / "handler.py").write_text(handler_source, encoding="utf-8")
    return package


def test_tool_contract_and_audit_record_are_enforced():
    router = ToolRouter()
    router.register(_manifest(), lambda payload: {"documents": [{"title": payload["query"]}]})

    result = router.call("researcher", "search_web", {"query": "售后争议"})

    assert result["documents"]
    assert router.audits[0].status == "succeeded"
    assert router.audits[0].input_hash
    assert router.audits[0].output_summary == {"keys": ["documents"], "item_counts": {"documents": 1}}


def test_provider_call_guard_wraps_each_concrete_external_provider():
    guarded = []
    router = ToolRouter(provider_call_guard=lambda provider, capability, payload, timeout, operation: (
        guarded.append((provider, capability, payload["query"], timeout)), operation(),
    )[1])
    router.register(_manifest(), lambda payload: {"documents": [{"title": payload["query"]}]})

    result = router.call("researcher", "search_web", {"query": "并发限流"})

    assert result["documents"][0]["title"] == "并发限流"
    assert guarded[0][:3] == ("test-search", "search_web", "并发限流")
    assert guarded[0][3] > 0


def test_invalid_input_and_output_are_rejected():
    router = ToolRouter()
    router.register(_manifest(), lambda payload: {"unexpected": True})

    with pytest.raises(ToolContractError, match="output (missing field: documents|has unknown fields: unexpected)"):
        router.call("researcher", "search_web", {"query": "x"})
    with pytest.raises(ToolContractError, match="input missing field: query"):
        router.call("researcher", "search_web", {})


def test_missing_requirements_are_audited_and_never_fall_back():
    router = ToolRouter(config={"search_api_key": "configured"})
    router.register(_manifest("primary", priority=1, requires=("missing_key",)), lambda payload: {"documents": []})
    router.register(_manifest("fallback", priority=2), lambda payload: {"documents": [{"title": "cached"}]})

    with pytest.raises(ToolUnavailable, match="missing requirements"):
        router.call("researcher", "search_web", {"query": "x"})

    assert router.audits[-1].status == "unavailable"
    assert router.audits[-1].error_code == "missing_configuration"
    with pytest.raises(PermissionError):
        router.call("verifier", "search_web", {"query": "x"})


def test_unconfigured_provider_fails_without_a_local_fallback(tmp_path):
    provider_root = tmp_path / "providers"
    provider_root.mkdir()
    optional = provider_root / "optional"
    optional.mkdir()
    (optional / "manifest.yaml").write_text(
        "\n".join([
            "name: optional", "version: 1.0.0", "description: Optional provider.",
            "owners: [researcher]", "capabilities: [search_web]", "exposure: core",
            "exposure_reason: optional", "requires: [OPTIONAL_KEY]", "input:",
            "  query: string", "output:", "  documents: array", "entrypoint: handler.py:run",
        ]), encoding="utf-8"
    )
    (optional / "handler.py").write_text("def run(payload): return {'documents': [{'title': 'optional'}]}\n", encoding="utf-8")
    router = ToolRouter(config={}, provider_root=provider_root)
    with pytest.raises(ToolUnavailable, match="missing requirements"):
        router.call("researcher", "search_web", {"query": "x"})


def test_write_tool_is_blocked_and_run_tool_counts_are_not_global():
    router = ToolRouter()
    router.register(_manifest("writer", risk="write", exposure="deferred"), lambda payload: {"documents": []})

    with pytest.raises(PermissionError, match="write action"):
        router.call("researcher", "search_web", {"query": "x"})

    router = ToolRouter(policy=InvestigationPolicy())
    router.register(_manifest("reader", max_calls=10), lambda payload: {"documents": []})
    router.call("researcher", "search_web", {"query": "x"})
    router.call("researcher", "search_web", {"query": "x"})


def test_provider_calls_are_isolated_per_investigation_run():
    class RunTrace:
        def __init__(self, run_id):
            self.run_id = run_id
            self.events = []

        def add_trace(self, step, status, **data):
            self.events.append((step, status, data))

    router = ToolRouter(policy=InvestigationPolicy())
    router.register(_manifest("scoped", max_calls=10), lambda payload: {"documents": []})

    router.call("researcher", "search_web", {"query": "one"}, run=RunTrace("run-a"))
    router.call("researcher", "search_web", {"query": "two"}, run=RunTrace("run-b"))

    router.call("researcher", "search_web", {"query": "again"}, run=RunTrace("run-a"))


def test_router_resolves_capability_to_registered_provider():
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="cached-search",
            description="Search a local public-source cache.",
            capabilities=("search_web",),
            owners=("researcher",),
            input_schema={"query": "string"},
            output_schema={"documents": "array"},
        ),
        lambda payload: {"documents": [{"title": payload["query"]}]},
    )

    result = router.call("researcher", "search_web", {"query": "x"})

    assert result["documents"][0]["title"] == "x"
    assert router.audits[0].capability == "search_web"


def test_provider_names_and_aliases_are_not_callable_capabilities():
    router = ToolRouter()
    router.register(_manifest("cached-search"), lambda payload: {"documents": []})

    with pytest.raises(ToolUnavailable, match="capability unavailable"):
        router.call("researcher", "cached-search", {"query": "x"})
    with pytest.raises(ToolContractError, match="unknown capabilities"):
        router.register(
            _manifest("alias-search", capabilities=("web_search",)),
            lambda payload: {"documents": []},
        )


def test_provider_manifest_requires_explicit_description_and_capabilities():
    with pytest.raises(ValueError, match="description is required"):
        ToolProviderManifest(name="missing-description", capabilities=("search_web",))
    with pytest.raises(ValueError, match="capabilities are required"):
        ToolProviderManifest(name="missing-capabilities", description="Missing capability contract.")


def test_failure_mode_controls_execution_fallback_and_attempt_group():
    router = ToolRouter()
    router.register(
        _manifest("primary", failure_mode="fallback", priority=1),
        lambda payload: (_ for _ in ()).throw(RuntimeError("upstream unavailable")),
    )
    router.register(_manifest("fallback", priority=2), lambda payload: {"documents": [{"title": "cached"}]})

    result = router.call("researcher", "search_web", {"query": "x"}, attempt_group_id="attempt-fixed")

    assert result["documents"][0]["title"] == "cached"
    assert router.audits[-2].status == "failed"
    assert router.audits[-1].status == "fallback_succeeded"
    assert router.audits[-1].fallback_from == "primary"
    assert router.audits[-1].attempt_group_id == "attempt-fixed"
    assert router.audits[-1].error_code is None
    assert router.audits[-1].previous_error_code == router.audits[-2].error_code == "execution_error"


def test_failed_fallback_keeps_previous_error_code():
    router = ToolRouter()
    router.register(
        _manifest("primary", failure_mode="fallback", priority=1),
        lambda payload: (_ for _ in ()).throw(RuntimeError("primary unavailable")),
    )
    router.register(
        _manifest("fallback", priority=2),
        lambda payload: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )

    with pytest.raises(RuntimeError, match="cache unavailable"):
        router.call("researcher", "search_web", {"query": "x"})

    assert router.audits[-1].status == "failed"
    assert router.audits[-1].fallback_from == "primary"
    assert router.audits[-1].previous_error_code == "execution_error"


def test_manifest_max_calls_and_priority_are_enforced():
    router = ToolRouter()
    calls = []
    router.register(
        ToolProviderManifest(
            name="preferred",
            description="Preferred replay search.",
            capabilities=("search_web",),
            owners=("researcher",),
            priority=1,
            max_calls=1,
            input_schema={"query": "string"},
            output_schema={"documents": "array"},
        ),
        lambda payload: (calls.append("preferred") or {"documents": []}),
    )
    router.register(
        ToolProviderManifest(
            name="backup",
            description="Backup replay search.",
            capabilities=("search_web",),
            owners=("researcher",),
            priority=2,
            input_schema={"query": "string"},
            output_schema={"documents": "array"},
        ),
        lambda payload: (calls.append("backup") or {"documents": []}),
    )

    router.call("researcher", "search_web", {"query": "one"})
    router.call("researcher", "search_web", {"query": "two"})

    assert calls == ["preferred", "backup"]
    assert any(item.status == "budget_exceeded" and item.provider == "preferred" for item in router.audits)


def test_tool_timeout_is_audited_and_returns_without_waiting_for_handler():
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="slow",
            description="A deliberately slow Tool provider.",
            capabilities=("search_web",),
            owners=("researcher",),
            timeout_seconds=0.01,
            input_schema={"query": "string"},
            output_schema={"documents": "array"},
        ),
        lambda payload: (sleep(0.2) or {"documents": []}),
    )

    with pytest.raises(Exception, match="exceeded timeout"):
        router.call("researcher", "search_web", {"query": "x"})

    assert router.audits[-1].status == "timed_out"
    assert router.audits[-1].error_code == "timeout"


def test_tool_provider_loader_reads_manifest_description():
    router = ToolRouter()
    package = Path(__file__).parents[2] / "echotrace" / "tool_providers" / "ingest-rss"

    manifest = router.load_package(package)

    assert manifest.description.startswith("Read a bounded public RSS")
    assert router.capability_detail("ingest_public_feed", "researcher")["description"].startswith("Ingest a public RSS")


def test_agent_startup_discovery_uses_default_provider_manifests():
    router = ToolRouter()
    router.ensure_discovered()
    context = router.tool_context("researcher")
    names = {item["capability"] for item in context["core_tools"] + context["deferred_tools"]}
    assert {"search_web", "fetch_public_page", "ingest_public_feed"}.issubset(names)


def test_tool_provider_package_rejects_skill_document(tmp_path):
    package = _write_package(
        tmp_path,
        "mixed-boundary",
        handler_source="def run(payload):\n    return {'documents': []}\n",
    )
    (package / "SKILL.md").write_text("not a Tool provider file", encoding="utf-8")

    with pytest.raises(ToolContractError, match="must not contain SKILL.md"):
        ToolRouter().load_package(package)


def test_tool_context_is_capability_first_and_does_not_leak_provider_details():
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="provider-a",
            description="Provider A implementation.",
            capabilities=("search_web",),
            owners=("researcher",),
            input_schema={"query": "string"},
            output_schema={"documents": "array"},
        ),
        lambda payload: {"documents": []},
    )

    context = router.tool_context("researcher")
    menu = context["core_tools"] + context["deferred_tools"]
    search = next(item for item in menu if item["capability"] == "search_web")

    assert "providers" not in search
    assert "provider-a" not in json.dumps(search)
    assert search["input_fields"][0]["name"] == "query"
    assert search["available"] is True
    assert "available_provider_count" not in search
    assert all(item["capability"] not in {"web_search", "query_mysql", "count_documents"} for item in menu)


def test_dynamic_tool_context_keeps_core_schema_and_defers_specialized_tools(tmp_path):
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="core-search",
            description="Core public search.",
            capabilities=("search_web",),
            owners=("researcher",),
            output_schema={"documents": "array", "count": "integer"},
            exposure="core",
            input_schema={"query": "string", "limit": "integer?", "domains": "string[]?"},
        ),
        lambda payload: {"documents": [], "count": 0},
    )
    router.register(
        ToolProviderManifest(
            name="deferred-discourse",
            description="Specialized discourse analysis.",
            capabilities=("analyze_discourse",),
            owners=("researcher",),
            output_schema={"features": "array", "coverage": "object", "count": "integer"},
            exposure="deferred",
            input_schema={"document_ids": "string[]"},
        ),
        lambda payload: {"features": [], "coverage": {}, "count": 0},
    )

    context = router.tool_context("researcher")
    assert [item["capability"] for item in context["core_tools"]] == ["search_web", "search_tools"]
    assert [item["capability"] for item in context["deferred_tools"]] == ["analyze_discourse"]
    assert context["core_tools"][0]["input_schema"]["required"] == ["query"]
    assert context["core_tools"][0]["output_schema"]["required"] == ["documents", "count"]
    loaded = router.tool_context("researcher", loaded_capabilities=("analyze_discourse",))
    assert [item["capability"] for item in loaded["loaded_tools"]] == ["analyze_discourse"]
    assert {item["capability"] for item in loaded["core_tools"]} == {"search_web", "analyze_discourse", "search_tools"}


def test_search_tools_is_a_core_gateway_meta_tool():
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="deferred-discourse",
            description="Specialized discourse analysis.",
            capabilities=("analyze_discourse",),
            owners=("researcher",),
            input_schema={"document_ids": "string[]"},
            output_schema={"features": "array", "coverage": "object", "count": "integer"},
            exposure="deferred",
        ),
        lambda payload: {"features": [], "coverage": {}, "count": 0},
    )
    native = router.native_tools("researcher")
    assert "search_tools" in {item["function"]["name"] for item in native}
    result = router.call("researcher", "search_tools", {"capabilities": ["analyze_discourse"]})
    assert result["count"] == 1
    assert result["tools"][0]["capability"] == "analyze_discourse"


def test_search_tools_requires_exact_capability_names_and_rejects_alias_fields():
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="deferred-discourse",
            description="Specialized discourse analysis.",
            capabilities=("analyze_discourse",),
            owners=("researcher",),
            input_schema={"document_ids": "string[]"},
            output_schema={"features": "array", "coverage": "object", "count": "integer"},
            exposure="deferred",
        ),
        lambda payload: {"features": [], "coverage": {}, "count": 0},
    )

    with pytest.raises(ToolContractError, match="unknown fields: max_results, query"):
        router.call("researcher", "search_tools", {"query": "discourse", "max_results": 1})
    with pytest.raises(ToolUnavailable, match="unknown deferred capability"):
        router.call("researcher", "search_tools", {"capabilities": ["analyze_discourse_typo"]})


def test_register_validates_output_contract_like_manifest_loading():
    router = ToolRouter()
    with pytest.raises(ToolContractError, match="output contract missing required field"):
        router.register(
            ToolProviderManifest(
                name="invalid-output",
                description="Invalid provider.",
                capabilities=("search_web",),
                owners=("researcher",),
                input_schema={"query": "string"},
                output_schema={},
            ),
            lambda payload: {"documents": []},
        )


def test_tool_context_is_manifest_driven_and_omits_uninstalled_catalog_capabilities():
    router = ToolRouter()
    router.register(
        _manifest("only-search"),
        lambda payload: {"documents": []},
    )
    context = router.tool_context("researcher")
    names = {item["capability"] for item in context["core_tools"] + context["deferred_tools"]}
    assert names == {"search_web", "search_tools"}


def test_provider_root_constructor_scans_manifests_without_importing_handlers(tmp_path):
    package = _write_package(
        tmp_path,
        "scanned-provider",
        handler_source="raise RuntimeError('must remain lazy')\ndef run(payload):\n    return {'documents': []}\n",
    )
    router = ToolRouter(provider_root=tmp_path)
    assert router.diagnostics()["load_errors"] == []
    assert router.tool_context("researcher")["core_tools"][0]["capability"] == "search_web"


def test_explicit_package_scan_root_is_not_merged_with_default_root(tmp_path):
    _write_package(
        tmp_path,
        "isolated-search",
        handler_source="def run(payload):\n    return {'documents': []}\n",
    )
    router = ToolRouter()
    router.load_packages(tmp_path)
    router.ensure_discovered()
    providers = {manifest.name for manifest in router.describe()}
    assert providers == {"isolated-search"}


def test_capability_agent_boundary_is_enforced_even_for_wildcard_provider():
    router = ToolRouter()
    router.register(
        ToolProviderManifest(
            name="wildcard-verifier",
            description="Verifier implementation.",
            capabilities=("check_source_independence",),
            owners=("*",),
            input_schema={"document_ids": "string[]"},
            output_schema={"independent": "boolean", "reason": "string"},
        ),
        lambda payload: {"independent": False, "reason": "pending"},
    )

    with pytest.raises(PermissionError, match="cannot request capability"):
        router.call("researcher", "check_source_independence", {"document_ids": ["doc-1"]})

    assert router.audits[-1].status == "denied"
    assert router.audits[-1].error_code == "capability_denied"


def test_package_discovery_is_lazy_until_first_call(tmp_path):
    package = tmp_path / "lazy-provider"
    package.mkdir()
    (package / "manifest.yaml").write_text(
        "\n".join(
            [
                "name: lazy-provider",
                "version: 1.0.0",
                "description: A lazy test provider.",
                "owners: [researcher]",
                "capabilities: [search_web]",
                "exposure: core",
                "exposure_reason: High-frequency test capability.",
                "input:",
                "  query: string",
                "output:",
                "  documents: array",
                "entrypoint: scripts/run.py:run",
            ]
        ),
        encoding="utf-8",
    )
    scripts = package / "scripts"
    scripts.mkdir()
    marker = tmp_path / "imported.txt"
    (scripts / "run.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\ndef run(payload):\n    return {{'documents': []}}\n",
        encoding="utf-8",
    )

    router = ToolRouter()
    router.load_package(package)
    assert not marker.exists()
    router.call("researcher", "search_web", {"query": "x"})
    assert marker.read_text(encoding="utf-8") == "imported"


def test_lazy_handler_import_failure_is_audited_without_leaking_cause(tmp_path):
    package = _write_package(
        tmp_path,
        "broken-import",
        handler_source="raise RuntimeError('secret provider token')\n",
    )
    router = ToolRouter()
    router.load_package(package)

    with pytest.raises(ToolUnavailable, match="failed to load Tool handler"):
        router.call("researcher", "search_web", {"query": "x"})

    audit = router.audits[-1]
    assert audit.status == "failed"
    assert audit.error_code == "handler_load_failed"
    assert audit.input_hash
    assert "secret provider token" not in audit.error


def test_bulk_loader_quarantines_invalid_fallback_and_keeps_valid_packages(tmp_path):
    _write_package(
        tmp_path,
        "broken-fallback",
        handler_source="def run(payload):\n    return {'documents': []}\n",
        fallback_providers=("missing-provider",),
    )
    _write_package(
        tmp_path,
        "valid-search",
        handler_source="def run(payload):\n    return {'documents': [{'title': payload['query']}]}\n",
    )
    router = ToolRouter()

    manifests = router.load_packages(tmp_path)
    result = router.call("researcher", "search_web", {"query": "kept"})

    assert [manifest.name for manifest in manifests] == ["valid-search"]
    assert result["documents"][0]["title"] == "kept"
    assert router.load_errors == [
        {
            "package": "broken-fallback",
            "error_code": "invalid_fallback",
            "error": "Tool provider broken-fallback references unknown fallback: missing-provider",
        }
    ]


def test_bulk_loader_quarantines_dependants_of_invalid_fallback_without_keyerror(tmp_path):
    _write_package(
        tmp_path,
        "broken-fallback",
        handler_source="def run(payload):\n    return {'documents': []}\n",
        fallback_providers=("missing-provider",),
    )
    _write_package(
        tmp_path,
        "dependent-provider",
        handler_source="def run(payload):\n    return {'documents': []}\n",
        fallback_providers=("broken-fallback",),
    )
    _write_package(
        tmp_path,
        "valid-search",
        handler_source="def run(payload):\n    return {'documents': [{'title': payload['query']}] }\n",
    )

    router = ToolRouter()
    manifests = router.load_packages(tmp_path)

    assert [manifest.name for manifest in manifests] == ["valid-search"]
    assert {item["package"] for item in router.load_errors} == {
        "broken-fallback",
        "dependent-provider",
    }
