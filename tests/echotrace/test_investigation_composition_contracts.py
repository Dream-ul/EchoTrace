from __future__ import annotations

import json

import pytest

from app.investigation_composition import (
    _available_node_capabilities,
    _make_model_transport_guard,
    _prepare_researcher_contract,
    _validate_conclusion_formatter_answer,
    _validate_orchestrator_classification,
    _validate_orchestrator_tasks,
)
from echotrace.agent.context_budget import estimate_tokens
from echotrace.domain.trace_nodes import NodeDraft, TraceContractError
from echotrace.runtime.tool_gateway import ToolProviderManifest, ToolRouter


def _context() -> dict:
    return {
        "trace_template": {
            "template_id": "public_event.trace",
            "template_version": "1.0.0",
            "node_specs": [
                {"slot_id": "origin_early_signals", "depends_on_slot_ids": []},
                {"slot_id": "event_scope", "depends_on_slot_ids": []},
            ],
        },
        "available_capabilities": ["search_web"],
        "node_targets": [
            {"slot_id": "origin_early_signals"},
            {"slot_id": "event_scope"},
        ]
    }


def _task(slot_id: str, priority: int = 0) -> dict:
    return {
        "slot_id": slot_id,
        "question": f"研究 {slot_id}",
        "capability": "search_web",
        "priority": priority,
    }


def test_orchestrator_classification_contract_requires_registered_type_and_rationale():
    context = {
        "investigation_type_candidates": [
            {"investigation_type": "public_event", "display_name": "公共事件", "purpose": "事件调查"},
            {"investigation_type": "public_opinion", "display_name": "舆情", "purpose": "舆情变化"},
        ]
    }
    _validate_orchestrator_classification(
        {"investigation_type": "public_opinion", "rationale": "目标关注舆情变化。"}, context
    )
    with pytest.raises(TraceContractError):
        _validate_orchestrator_classification(
            {"investigation_type": "public_opinion", "rationale": "目标关注舆情变化。", "extra": 1}, context
        )
    with pytest.raises(TraceContractError):
        _validate_orchestrator_classification(
            {"investigation_type": "unknown", "rationale": "目标关注舆情变化。"}, context
        )
    with pytest.raises(TraceContractError):
        _validate_orchestrator_classification(
            {"investigation_type": "public_opinion", "rationale": "x" * 501}, context
        )


def test_node_planning_capabilities_match_configured_research_providers():
    router = ToolRouter(config={"SEARCH_KEY": "configured"})
    router.register(
        ToolProviderManifest(
            name="configured-search", description="Public search.",
            capabilities=("search_web",), owners=("researcher",),
            input_schema={"query": "string"}, output_schema={"documents": "array"},
            requires=("SEARCH_KEY",),
        ),
        lambda payload: {"documents": []},
    )
    router.register(
        ToolProviderManifest(
            name="disabled-fetch", description="Public page fetch.",
            capabilities=("fetch_public_page",), owners=("researcher",),
            input_schema={"url": "string"}, output_schema={"document": "object"},
            requires=("FETCH_ENABLED",),
        ),
        lambda payload: {"document": {}},
    )
    router.register(
        ToolProviderManifest(
            name="rss", description="Public RSS ingestion.",
            capabilities=("ingest_public_feed",), owners=("researcher",),
            input_schema={"feed_url": "string", "limit": "integer?"},
            output_schema={"documents": "array", "count": "integer"},
            exposure="deferred",
        ),
        lambda payload: {"documents": [], "count": 0},
    )
    router.register(
        ToolProviderManifest(
            name="normalizer", description="Document normalization.",
            capabilities=("normalize_documents",), owners=("researcher",),
            input_schema={"records": "array"},
            output_schema={"documents": "array", "count": "integer"},
            exposure="deferred",
        ),
        lambda payload: {"documents": [], "count": 0},
    )

    assert _available_node_capabilities(router) == ["ingest_public_feed", "search_web"]


def _proposal(tasks: list[dict]) -> dict:
    return {
        "template_id": "public_event.trace",
        "template_version": "1.0.0",
        "tasks": tasks,
    }


def test_live_orchestrator_contract_requires_each_target_once():
    _validate_orchestrator_tasks(
        _proposal([_task("origin_early_signals"), _task("event_scope")]),
        _context(),
    )


def test_researcher_contract_prepares_application_owned_link_ids_before_validation():
    payload = {
        "node_id": "node-1",
        "slot_id": "origin_early_signals",
        "revision": 0,
        "claims": [{
            "claim_type": "observation",
            "text": "多份原始材料显示事件最早于当日公开。",
            "source_document_ids": ["doc-1"],
            "evidence_ids": ["evidence-1"],
        }],
        "evidence": [{
            "evidence_id": "evidence-1",
            "document_id": "doc-1",
            "excerpt": "该来源于当日首次公开相关情况。",
            "locator": {"url": "https://example.test/source"},
        }],
        "conclusion": "现有来源可将最早公开时间定位到当日。",
        "media": [],
    }

    _prepare_researcher_contract(payload)
    draft = NodeDraft.from_dict(payload)

    assert draft.claims[0].claim_id == "model-claim-0"
    assert draft.evidence[0].claim_id == draft.claims[0].claim_id


def test_researcher_contract_restores_missing_claim_links_from_evidence_claim_id():
    payload = {
        "claims": [{
            "claim_id": "claim-1",
            "claim_type": "observation",
            "text": "来源记录了事件公开时间。",
            "source_document_ids": ["doc-1"],
        }],
        "evidence": [{
            "evidence_id": "evidence-1",
            "claim_id": "claim-1",
            "document_id": "doc-1",
            "excerpt": "该来源于当日首次公开相关情况。",
            "locator": {"url": "https://example.test/source"},
        }],
        "conclusion": "现有来源可定位事件公开时间。",
        "media": [],
    }

    _prepare_researcher_contract(payload)
    draft = NodeDraft.from_dict(payload | {"node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0})

    assert draft.claims[0].evidence_ids == ["evidence-1"]
    assert draft.evidence[0].claim_id == "claim-1"


def test_researcher_contract_only_assigns_unlinked_evidence_when_claim_is_unambiguous():
    payload = {
        "claims": [{
            "claim_type": "observation",
            "text": "来源记录了事件公开时间。",
            "source_document_ids": ["doc-1"],
        }],
        "evidence": [{
            "document_id": "doc-1",
            "excerpt": "该来源于当日首次公开相关情况。",
            "locator": {"url": "https://example.test/source"},
        }],
    }

    _prepare_researcher_contract(payload)

    assert payload["claims"][0]["evidence_ids"] == ["model-evidence-0"]
    assert payload["evidence"][0]["claim_id"] == "model-claim-0"


def test_researcher_contract_does_not_invent_missing_claim_content():
    payload = {
        "claims": [{"evidence_ids": ["evidence-1"]}],
        "evidence": [{
            "evidence_id": "evidence-1",
            "document_id": "doc-1",
            "excerpt": "原始材料片段。",
            "locator": {"url": "https://example.test/source"},
        }],
    }

    _prepare_researcher_contract(payload)

    assert "claim_id" in payload["claims"][0]
    assert "claim_type" not in payload["claims"][0]
    assert "text" not in payload["claims"][0]


def test_conclusion_formatter_contract_requires_exact_identity_and_ir():
    payload = {
        "node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0,
        "conclusion_ir": {
            "schema_version": "conclusion-ir.v1",
            "blocks": [{"type": "paragraph", "text": "完整结论。"}],
        },
    }
    _validate_conclusion_formatter_answer(payload, {"node_task": {"node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0}})
    with pytest.raises(TraceContractError, match="exactly"):
        _validate_conclusion_formatter_answer({**payload, "extra": True}, {"node_task": {"node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0}})


def test_conclusion_formatter_contract_allows_editorial_rewriting():
    payload = {
        "node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0,
        "conclusion_ir": {
            "schema_version": "conclusion-ir.v1",
            "blocks": [{
                "type": "heading", "level": 2, "text": "处置进展",
            }, {
                "type": "list", "ordered": True, "items": ["有关部门发布了复核结果。"],
            }],
        },
    }
    context = {
        "node_task": {"node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0},
        "conclusion": "第一，相关部门发布了复核结果。详见 nclaim_abcd1234。",
    }

    _validate_conclusion_formatter_answer(payload, context)


def test_conclusion_formatter_contract_rejects_internal_artifacts():
    conclusion = "Section heading: body details."
    payload = {
        "node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0,
        "conclusion_ir": {
            "schema_version": "conclusion-ir.v1",
            "blocks": [{
                "type": "paragraph", "text": "结论字段 discussion_trend, limitations",
            }],
        },
    }
    context = {
        "node_task": {"node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0},
        "conclusion": conclusion,
    }

    with pytest.raises(TraceContractError, match="internal identifier or field name"):
        _validate_conclusion_formatter_answer(payload, context)


def test_conclusion_formatter_contract_rejects_agent_generated_provenance_fields():
    payload = {
        "node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0,
        "conclusion_ir": {
            "schema_version": "conclusion-ir.v1",
            "blocks": [{"type": "paragraph", "text": "结论。", "source_fragment": "旧版原文"}],
        },
    }

    with pytest.raises(TraceContractError, match="legacy source_fragment"):
        _validate_conclusion_formatter_answer(payload, {
            "node_task": {"node_id": "node-1", "slot_id": "origin_early_signals", "revision": 0},
        })


def test_live_orchestrator_contract_rejects_extra_top_level_fields():
    proposal = _proposal([_task("origin_early_signals"), _task("event_scope")])
    proposal["summary"] = "unrequested wrapper field"

    with pytest.raises(TraceContractError, match="exactly template_id, template_version and tasks"):
        _validate_orchestrator_tasks(proposal, _context())


def test_live_orchestrator_contract_rejects_template_identity_drift():
    proposal = _proposal([_task("origin_early_signals"), _task("event_scope")])
    proposal["template_version"] = "2.0.0"

    with pytest.raises(TraceContractError, match="preserve the pinned template identity"):
        _validate_orchestrator_tasks(proposal, _context())


def test_live_orchestrator_contract_rejects_unsupported_task_fields():
    task = _task("origin_early_signals")
    task["rationale"] = "uncontracted"
    proposal = _proposal([task, _task("event_scope")])

    with pytest.raises(TraceContractError, match="unsupported fields"):
        _validate_orchestrator_tasks(proposal, _context())


def test_live_orchestrator_contract_rejects_dependency_cycles():
    first = _task("origin_early_signals")
    second = _task("event_scope")
    first["depends_on_slot_ids"] = ["event_scope"]
    second["depends_on_slot_ids"] = ["origin_early_signals"]

    with pytest.raises(TraceContractError, match="dependencies contain a cycle"):
        _validate_orchestrator_tasks(_proposal([first, second]), _context())


def test_model_transport_guard_estimates_serialized_messages_and_tools(monkeypatch):
    monkeypatch.setenv("ECHOTRACE_LLM_RESERVED_OUTPUT_TOKENS", "600")
    monkeypatch.setenv("ECHOTRACE_LLM_REQUESTS_PER_MINUTE", "60")
    monkeypatch.setenv("ECHOTRACE_LLM_TOKENS_PER_MINUTE", "200000")
    monkeypatch.setenv("ECHOTRACE_LLM_PROVIDER_MAX_CONCURRENT", "8")
    monkeypatch.setenv("ECHOTRACE_LLM_ORCHESTRATOR_MAX_CONCURRENT", "3")
    observed = {}

    class Coordinator:
        def execute(self, operation, **kwargs):
            observed.update(kwargs)
            return operation()

    payload = {
        "messages": [{"role": "user", "content": "调查公共事件"}],
        "tools": [{"type": "function", "function": {"name": "search_web"}}],
        "max_tokens": 1200,
    }
    body = json.dumps(
        {"messages": payload["messages"], "tools": payload["tools"]},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    called = []
    guard = _make_model_transport_guard(Coordinator(), "orchestrator")

    result = guard("https://model.example/v1", "secret", payload, lambda: called.append(True) or "ok", 10)

    assert result == "ok"
    assert called == [True]
    assert observed["request_cost"] == estimate_tokens(body) + 600
    assert observed["credential"] == "secret"
    assert observed["scopes"][1] == ("role", "orchestrator", 3)


def test_model_transport_guard_default_token_window_accepts_context_sized_request(monkeypatch):
    monkeypatch.delenv("ECHOTRACE_LLM_TOKENS_PER_MINUTE", raising=False)
    monkeypatch.setenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "1000000")
    monkeypatch.setenv("ECHOTRACE_LLM_RESERVED_OUTPUT_TOKENS", "6000")
    observed = {}

    class Coordinator:
        def execute(self, operation, **kwargs):
            observed.update(kwargs)
            return operation()

    payload = {"messages": [{"role": "user", "content": "x" * 750_000}], "tools": []}
    guard = _make_model_transport_guard(Coordinator(), "researcher")

    result = guard("https://model.example/v1", "secret", payload, lambda: "ok", 10)

    assert result == "ok"
    assert observed["token_limit"] == 1_000_000
    assert observed["request_cost"] > 200_000


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ([_task("origin_early_signals"), _task("origin_early_signals")], "重复 slot_id"),
        ([_task("origin_early_signals")], "遗漏 slot_id=event_scope"),
        ([_task("origin_early_signals"), _task("event_scope"), _task("other")], "未分配 slot_id=other"),
    ],
)
def test_live_orchestrator_contract_rejects_wrong_slot_coverage(tasks, message):
    with pytest.raises(TraceContractError, match=message):
        _validate_orchestrator_tasks(_proposal(tasks), _context())


def test_live_orchestrator_contract_rejects_invalid_priority_with_context():
    proposal = _proposal([_task("origin_early_signals"), _task("event_scope")])
    proposal["tasks"][1]["priority"] = "high"

    with pytest.raises(TraceContractError, match="priority 必须是非负整数"):
        _validate_orchestrator_tasks(proposal, _context())
