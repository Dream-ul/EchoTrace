from __future__ import annotations

import json
import time

import pytest
from pathlib import Path

from echotrace.agent import AgentAction, AgentRuntime, AgentSkillCatalog, ReplayModelClient
from echotrace.agent.context import AgentContextAssembler
from echotrace.agent.context_pack import build_context_pack
from echotrace.agent.model_clients import ModelRequestError, ModelResponseError, OpenAICompatibleModelClient
from echotrace.agent.diagnostics import JsonlDiagnostics
from echotrace.runtime.tool_gateway import ToolRouter


def _client(transport, *, max_retries=2, deadline=None, max_tokens=32768):
    return OpenAICompatibleModelClient(
        base_url="https://model.example/v1",
        api_key="test-key",
        model_name="test-model",
        transport=transport,
        max_retries=max_retries,
        deadline=deadline,
        max_tokens=max_tokens,
    )


def test_request_retries_transient_transport_failure_with_a_hard_bound():
    calls = []

    def transport(*_args):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("temporary gateway failure")
        return {"choices": [{"message": {"content": "{}"}}]}

    result = _client(transport)._request({"model": "test-model"})

    assert result["choices"]
    assert len(calls) == 3


def test_request_transport_guard_wraps_the_actual_provider_attempt():
    calls = []

    def transport(*_args):
        calls.append("transport")
        return {"choices": [{"message": {"content": "{}"}}]}

    def guard(base_url, api_key, payload, send, timeout_seconds):
        calls.append((base_url, api_key, payload["model"], timeout_seconds > 0))
        return send()

    client = OpenAICompatibleModelClient(
        base_url="https://model.example/v1",
        api_key="test-key",
        model_name="test-model",
        transport=transport,
        transport_guard=guard,
    )

    client._request({"model": "test-model"})

    assert calls[0] == ("https://model.example/v1", "test-key", "test-model", True)
    assert calls[1] == "transport"


def test_provider_error_diagnostic_keeps_sanitized_type_error_detail(tmp_path):
    diagnostics = JsonlDiagnostics(tmp_path)

    def transport(*_args):
        raise TypeError("estimate_tokens expected str, got list")

    client = OpenAICompatibleModelClient(
        base_url="https://model.example/v1",
        api_key="test-key",
        model_name="test-model",
        transport=transport,
        diagnostics=diagnostics,
    )

    with pytest.raises(ModelRequestError):
        client._request({"model": "test-model", "messages": []})

    record = json.loads((tmp_path / "provider-errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert "estimate_tokens expected str, got list" in record["message_summary"]


def test_request_stops_after_configured_retry_budget():
    calls = []

    def transport(*_args):
        calls.append(1)
        raise OSError("gateway unavailable")

    with pytest.raises(ModelResponseError, match="after 3 attempts"):
        _client(transport, max_retries=2)._request({"model": "test-model"})

    assert len(calls) == 3


def test_request_does_not_start_after_task_deadline():
    calls = []

    def transport(*_args):
        calls.append(1)
        return {"choices": [{"message": {"content": "{}"}}]}

    with pytest.raises(ModelResponseError, match="deadline exceeded"):
        _client(transport, deadline=time.monotonic() - 0.001)._request({"model": "test-model"})

    assert calls == []


def test_provider_content_rejection_is_not_retried():
    calls = []

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(*_args):
        calls.append(1)
        raise Rejected("inappropriate content")

    with pytest.raises(ModelRequestError) as caught:
        _client(transport, max_retries=2)._request({"model": "test-model"})

    assert caught.value.retryable is False
    assert caught.value.error_code == "provider_policy_rejected"
    assert len(calls) == 1


def test_provider_diagnostics_write_bounded_error_and_turn_records(tmp_path):
    diagnostics = JsonlDiagnostics(tmp_path, retention_days=None)
    calls = []

    class Rejected(Exception):
        code = "context_length_exceeded"
        status_code = 413

    def transport(*_args):
        calls.append(1)
        raise Rejected("context too large; body=full prompt should not be logged")

    client = OpenAICompatibleModelClient(
        base_url="https://model.example/v1",
        api_key="secret-api-key",
        model_name="test-model",
        transport=transport,
        max_retries=0,
        diagnostics=diagnostics,
    )
    with pytest.raises(ModelRequestError) as caught:
        client._request({
            "model": "test-model",
            "messages": [{"role": "user", "content": "full source body"}],
        })

    assert caught.value.error_code == "provider_context_length"
    lines = (tmp_path / "provider-errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["error_code"] == "provider_context_length"
    assert record["http_status"] == 413
    assert "full source body" not in lines[0]
    assert "secret-api-key" not in lines[0]


def test_successful_provider_turn_is_logged_with_hash_not_response_content(tmp_path):
    diagnostics = JsonlDiagnostics(tmp_path, retention_days=None)

    def transport(*_args):
        return {"id": "provider-req-1", "choices": [{"message": {"content": '{"action":"final"}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3}}

    client = OpenAICompatibleModelClient(
        base_url="https://model.example/v1",
        api_key="test-key",
        model_name="test-model",
        transport=transport,
        diagnostics=diagnostics,
    )
    client._request({"model": "test-model", "messages": [{"role": "user", "content": "task"}]})

    line = (tmp_path / "agent-turns.jsonl").read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["response_hash"].startswith("sha256:")
    assert record["finish_reason"] == "stop"
    assert "action" not in line


def test_provider_content_rejection_gets_one_source_text_sanitized_retry(tmp_path):
    payloads = []
    diagnostics = JsonlDiagnostics(tmp_path, retention_days=None)

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise Rejected("inspection failed")
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100},
        }

    client = OpenAICompatibleModelClient(
        base_url="https://model.example/v1", api_key="test-key", model_name="test-model",
        transport=transport, max_retries=0, diagnostics=diagnostics,
    )
    result = client._request({
        "model": "test-model",
        "max_tokens": 1000,
        "messages": [{
            "role": "user",
            "content": json.dumps({"source_documents": [{
                "document_id": "doc-1", "url": "https://example.test", "content": "原始来源正文" * 1000,
            }]}, ensure_ascii=False),
        }],
    })

    assert result["choices"]
    assert len(payloads) == 2
    assert "原始来源正文" in payloads[0]["messages"][0]["content"]
    assert "原始来源正文" not in payloads[1]["messages"][0]["content"]
    assert "doc-1" in payloads[1]["messages"][0]["content"]
    assert "https://example.test" in payloads[1]["messages"][0]["content"]
    rejected = json.loads((tmp_path / "provider-errors.jsonl").read_text(encoding="utf-8").splitlines()[0])
    accepted = json.loads((tmp_path / "agent-turns.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert accepted["input_tokens_estimated"] < rejected["input_tokens_estimated"]
    assert accepted["usage"]["prompt_tokens"] == 100


def test_provider_content_rejection_gets_bounded_source_context_retry():
    payloads = []

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) < 3:
            raise Rejected("inspection failed")
        return {"choices": [{"message": {"content": "ok"}}]}

    client = _client(transport, max_retries=0)
    result = client._request({
        "model": "test-model",
        "messages": [{
            "role": "user",
            "content": json.dumps({
                "source_documents": [{"document_id": "doc-1", "title": "sensitive title", "content": "raw"}],
                "current_draft": {"claims": [{"text": "sensitive claim"}], "evidence": [{"quote": "raw quote"}]},
            }),
        }],
    })

    assert result["choices"]
    assert len(payloads) == 3
    assert "raw quote" not in payloads[1]["messages"][0]["content"]
    assert "sensitive claim" not in payloads[2]["messages"][0]["content"]
    assert "sensitive title" not in payloads[2]["messages"][0]["content"]
    assert "doc-1" in payloads[2]["messages"][0]["content"]


def test_provider_recovery_detects_and_redacts_verifier_context_pack_source_summaries():
    payloads = []

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) < 3:
            raise Rejected("inspection failed")
        return {"choices": [{"message": {"content": "ok"}}]}

    client = _client(transport, max_retries=0)
    context_pack = build_context_pack("verifier", {
        "run_id": "run-1",
        "node_task": {"node_id": "node-1", "slot_id": "initial_spread", "revision": 2},
        "current_draft": {
            "node_id": "node-1",
            "slot_id": "initial_spread",
            "revision": 2,
            "conclusion": "Review the current node draft.",
            "claims": [{"claim_id": "claim-1", "text": "Keep this claim for review.", "evidence_ids": ["evidence-1"]}],
            "evidence": [{
                "evidence_id": "evidence-1",
                "document_id": "doc-1",
                "excerpt": "draft evidence excerpt to omit",
                "locator": {"url": "https://example.test/source"},
                "relation": "supports",
            }],
        },
        "source_documents": [{
            "document_id": "doc-1",
            "source": "news",
            "title": "source title",
            "content": "source content to omit",
            "url": "https://example.test/source",
            "content_hash": "sha256:source",
        }],
    })
    original = {"context_pack": context_pack}
    evidence = context_pack["evidence"]
    assert evidence[0]["summary"]
    assert evidence[0]["document_card"]["relevant_excerpts"][0]["text"] == "source content to omit"
    client._request({
        "model": "test-model",
        "messages": [{"role": "user", "content": json.dumps(original, ensure_ascii=False)}],
    })

    assert len(payloads) == 3
    sanitized = json.loads(payloads[1]["messages"][0]["content"])
    stripped = json.loads(payloads[2]["messages"][0]["content"])
    assert "source content to omit" not in payloads[1]["messages"][0]["content"]
    assert "draft evidence excerpt to omit" not in payloads[1]["messages"][0]["content"]
    assert sanitized["context_pack"]["state"]["current_draft"]["claims"][0]["text"] == "[omitted after provider content inspection]"
    assert "source content to omit" not in payloads[2]["messages"][0]["content"]
    assert "draft evidence excerpt to omit" not in payloads[2]["messages"][0]["content"]
    assert stripped["context_pack"]["evidence"][0]["document_card"]["document_id"] == "doc-1"
    assert any(item.get("citation", {}).get("evidence_id") == "evidence-1" for item in stripped["context_pack"]["evidence"])


def test_provider_recovery_redacts_formatter_claims_and_conclusion_text():
    payloads = []

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) < 3:
            raise Rejected("inspection failed")
        return {"choices": [{"message": {"content": "ok"}}]}

    context_pack = build_context_pack("conclusion_formatter", {
        "run_id": "run-1",
        "current_draft": {"node_id": "node-1", "slot_id": "initial_spread", "revision": 2},
        "conclusion": "Sensitive source-derived conclusion",
        "claims": [{"claim_id": "claim-1", "text": "Sensitive source-derived claim"}],
        "evidence": [{"document_id": "doc-1", "excerpt": "Sensitive source excerpt"}],
        "media": [{"caption": "Sensitive media caption", "why_relevant": "Sensitive media context"}],
    })
    client = _client(transport, max_retries=0)
    client._request({
        "model": "test-model",
        "messages": [{"role": "user", "content": json.dumps({"context_pack": context_pack})}],
    })

    assert len(payloads) == 3
    sanitized = json.loads(payloads[1]["messages"][0]["content"])
    state = sanitized["context_pack"]["state"]
    assert state["conclusion"] == "[omitted after provider content inspection]"
    assert state["claims"][0]["text"] == "[omitted after provider content inspection]"
    assert state["evidence"][0]["excerpt"] == "[omitted after provider content inspection]"
    assert state["media"][0]["caption"] == "[omitted after provider content inspection]"
    assert state["media"][0]["why_relevant"] == "[omitted after provider content inspection]"
    assert "Sensitive source-derived" not in payloads[1]["messages"][0]["content"]
    assert "Sensitive media" not in payloads[2]["messages"][0]["content"]


def test_provider_source_recovery_projection_persists_across_agent_turns():
    payloads = []

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise Rejected("inspection failed")
        return {"choices": [{"message": {"content": '{"action":"final","answer":"ok"}'}, "finish_reason": "stop"}]}

    client = _client(transport, max_retries=0)
    first = {
        "model": "test-model",
        "messages": [{
            "role": "user",
            "content": json.dumps({"source_documents": [{
                "document_id": "doc-1", "url": "https://example.test", "content": "raw source text",
            }]}, ensure_ascii=False),
        }],
    }
    client._request(first)
    assert client._provider_source_projection == "sanitized"

    client.decide(first["messages"])
    latest = payloads[-1]["messages"][0]["content"]
    assert "raw source text" not in latest
    assert "doc-1" in latest

    client.start_session()
    assert client._provider_source_projection is None


def test_provider_refusal_after_sanitization_escalates_without_repeating_same_projection():
    payloads = []

    class Rejected(Exception):
        code = "data_inspection_failed"

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        raise Rejected("inspection failed")

    client = _client(transport, max_retries=0)
    client._provider_source_projection = "sanitized"
    with pytest.raises(ModelRequestError, match="provider rejected"):
        client._request({
            "model": "test-model",
            "messages": [{
                "role": "user",
                "content": json.dumps({"source_documents": [{
                    "document_id": "doc-1", "content": "already safe excerpt",
                }]}, ensure_ascii=False),
            }],
        })

    assert len(payloads) == 2
    assert client._provider_source_projection == "stripped"
    assert "already safe excerpt" in payloads[0]["messages"][0]["content"]
    assert "already safe excerpt" not in payloads[1]["messages"][0]["content"]
    assert "doc-1" in payloads[1]["messages"][0]["content"]


def test_native_state_snapshot_keeps_deferred_capability_index_after_first_turn():
    messages = AgentContextAssembler().build(
        role="researcher",
        objective="研究节点",
        skill_index=(),
        deferred_tools=[{
            "capability": "analyze_discourse",
            "description": "分析公开讨论",
            "use_when": ["需要舆情特征"],
            "avoid_when": ["不能替代事实证据"],
        }],
        include_tool_directory=False,
        native_tools=True,
        active_skill=None,
        skill_instructions=None,
        observations=[],
    )

    payload = json.loads(messages[1]["content"])
    assert payload["tool_directory"]["status"] == "native"
    assert payload["deferred_tools"][0]["capability"] == "analyze_discourse"


def test_native_repair_wraps_complete_business_json_in_final_answer():
    payloads = []
    business = '{"node_id":"node-1","slot_id":"origin_early_signals","revision":0,"claims":[],"evidence":[],"conclusion":"结论","media":[]}'

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return {"choices": [{"message": {"content": business}, "finish_reason": "stop"}]}
        return {"choices": [{"message": {"content": json.dumps({
            "action": "final", "summary": "交付节点业务载荷", "skill": None,
            "reference": None, "answer": business,
        }, ensure_ascii=False)}, "finish_reason": "stop"}]}

    client = _client(transport)
    client.start_session()
    decision = client.decide(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "snapshot"}],
        tools=[{"type": "function", "function": {"name": "agent_control"}}],
    )

    assert decision.action.value == "final"
    assert decision.answer == business
    assert len(payloads) == 1


def test_native_truncated_output_gets_larger_bounded_repair_budget():
    payloads = []
    repaired = json.dumps({
        "action": "final", "summary": "完成", "skill": None,
        "reference": None, "answer": "{}",
    })

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return {"choices": [{"message": {"content": '{"node_id":"node-1"'}, "finish_reason": "length"}]}
        return {"choices": [{"message": {"content": repaired}, "finish_reason": "stop"}]}

    client = _client(transport)
    client.start_session()
    decision = client.decide(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "snapshot"}],
        tools=[{"type": "function", "function": {"name": "agent_control"}}],
    )

    assert decision.action.value == "final"
    assert payloads[1]["max_tokens"] > payloads[0]["max_tokens"]
    assert "被截断" in payloads[1]["messages"][-1]["content"]


def test_role_contract_failure_reasks_within_the_same_agent_session():
    model = ReplayModelClient([
        {"action": "final", "summary": "bad", "answer": "bad"},
        {"action": "final", "summary": "good", "answer": "good"},
    ])
    attempts = []

    def validate(answer: str) -> str:
        attempts.append(answer)
        if answer == "bad":
            raise ValueError("NodeDraft.evidence is required")
        return answer

    runtime = AgentRuntime(
        model,
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=2,
        validate_answer=validate,
        max_contract_retries=1,
    )

    result = runtime.run("researcher", "填写节点")

    assert result.status == "completed"
    assert attempts == ["bad", "good"]


def test_native_session_replaces_old_state_snapshots_but_keeps_tool_transcript():
    payloads = []

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        call_id = f"call-{len(payloads)}"
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "search_web",
                            "arguments": '{"query":"node evidence"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

    client = _client(transport, max_tokens=100)
    client.start_session()
    tools = [{"type": "function", "function": {"name": "search_web"}}]
    client.decide(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "snapshot-1"}],
        tools=tools,
    )
    client.record_result({"status": "succeeded", "output": {"documents": [{"document_id": "doc-1"}]}})
    client.decide(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "snapshot-2"}],
        tools=tools,
    )

    assert len(payloads) == 2
    second_messages = payloads[1]["messages"]
    assert [message["role"] for message in second_messages] == ["system", "assistant", "tool", "user"]
    assert "snapshot-1" not in {message.get("content") for message in second_messages}
    assert second_messages[-1]["content"] == "snapshot-2"
    assert second_messages[2]["tool_call_id"] == "call-1"


def test_native_session_compacts_old_tool_results_before_next_request(monkeypatch):
    monkeypatch.setenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "1000")
    payloads = []

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        call_id = f"call-{len(payloads)}"
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "search_web", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

    client = _client(transport, max_tokens=100)
    client.start_session()
    tools = [{"type": "function", "function": {"name": "search_web"}}]
    client.decide([{"role": "system", "content": "system"}, {"role": "user", "content": "snapshot-1"}], tools=tools)
    client.record_result({"status": "succeeded", "output": {"content": "large-source-text" * 700}})
    client.decide([{"role": "system", "content": "system"}, {"role": "user", "content": "snapshot-2"}], tools=tools)

    assert client.last_compaction is not None
    assert client.last_compaction["applied"] is True
    assert client.last_compaction["cleared_tool_results"] == 1
    second_tool = next(item for item in payloads[1]["messages"] if item.get("role") == "tool")
    assert "large-source-text" not in second_tool["content"]
    assert "result_ref" in second_tool["content"]


def test_context_budget_exceeded_does_not_call_transport(monkeypatch):
    monkeypatch.setenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "100")
    calls = []

    def transport(*_args):
        calls.append(1)
        return {"choices": [{"message": {"content": "{}"}}]}

    client = _client(transport, max_tokens=10)
    client.start_session()
    with pytest.raises(ModelRequestError) as caught:
        client.decide([
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 5000},
        ])

    assert caught.value.error_code == "context_budget_exceeded"
    assert calls == []
    assert client.last_budget_report is not None
    assert client.last_budget_report.over_hard_limit is True


def test_context_pack_is_reduced_before_provider_request(monkeypatch):
    monkeypatch.setenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "32000")
    documents = [
        {
            "document_id": f"doc-{index}",
            "source": "official",
            "title": f"公告 {index}",
            "url": f"https://example.test/{index}",
            "content": "来源原文片段" * 400,
            "content_hash": f"sha256:{index}",
        }
        for index in range(12)
    ]
    current_draft = {
        "node_id": "node-1",
        "slot_id": "response",
        "revision": 0,
        "conclusion": "节点综合结论" * 100,
        "claims": [{
            "claim_id": f"claim-{index}", "claim_type": "fact", "text": "归纳主张" * 50,
            "source_document_ids": [f"doc-{index % 12}"], "evidence_ids": [f"evidence-{index}"],
        } for index in range(24)],
        "evidence": [{
            "evidence_id": f"evidence-{index}", "claim_id": f"claim-{index}",
            "document_id": f"doc-{index % 12}", "excerpt": "可复核原文片段" * 100,
            "locator": {"url": f"https://example.test/{index % 12}"},
        } for index in range(24)],
    }
    pack = build_context_pack("researcher", {
        "run_id": "run-context-budget",
        "execution_task_id": "task-context-budget",
        "objective": "调查指定事件的经过与处置结果",
        "task": {"node_id": "node-1", "slot_id": "response", "revision": 1},
        "node_spec": {"title": "响应与处置", "goal": "还原行动主体、行动和结果"},
        "current_draft": current_draft,
        "source_documents": documents,
        "source_analysis_features": [{
            "feature_id": f"feature-{index}", "feature_type": "sentiment", "summary": "样本分析" * 80,
        } for index in range(16)],
    })
    prompt = [{
        "role": "system", "content": "研究当前节点，并通过工具获取仍需核对的来源。",
    }, {
        "role": "user", "content": json.dumps({"objective": "补全节点", "context_pack": pack}, ensure_ascii=False),
    }]
    payloads = []

    def transport(_base_url, _api_key, payload):
        payloads.append(payload)
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call-context",
                        "type": "function",
                        "function": {"name": "search_web", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

    client = _client(transport, max_tokens=1000)
    client.start_session()
    tools = [{"type": "function", "function": {
        "name": "search_web", "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }}]

    client.decide(prompt, tools=tools)

    assert len(payloads) == 1
    assert client.last_budget_report is not None
    assert client.last_budget_report.input_tokens_estimated <= client.last_budget_report.hard_limit_tokens
    assert client.last_compaction is not None
    assert client.last_compaction["context_pack_compaction"]["applied"] is True
    packed_user = json.loads(next(item["content"] for item in payloads[0]["messages"] if item["role"] == "user"))
    compacted = packed_user["context_pack"]
    assert compacted["pack_hash"] == client.last_budget_report.context_pack_hash
    assert compacted["state"]["current_draft"]["claims"]
    assert any(item.get("reason") == "context_budget" for item in compacted["omissions"])


def test_local_budget_error_diagnostic_counts_tool_schemas(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "100")
    diagnostics = JsonlDiagnostics(tmp_path)
    client = OpenAICompatibleModelClient(
        base_url="https://model.example/v1", api_key="test-key", model_name="test-model",
        transport=lambda *_args: pytest.fail("oversized request must not reach transport"),
        max_tokens=10, diagnostics=diagnostics,
    )
    client.start_session()

    with pytest.raises(ModelRequestError, match="context remains above"):
        client.decide([
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 5000},
        ], tools=[{"type": "function", "function": {"name": "tool", "parameters": {"type": "object"}}}])

    event = json.loads((tmp_path / "provider-errors.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert event["tool_schema_count"] == 1
    assert event["tool_schema_chars"] > 0
    assert event["error_code"] == "context_budget_exceeded"


def test_runtime_exposes_context_budget_exceeded_as_terminal_status(monkeypatch):
    monkeypatch.setenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "100")
    client = _client(lambda *_args: {"choices": [{"message": {"content": "{}"}}]}, max_tokens=10)
    runtime = AgentRuntime(
        client,
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=1,
    )

    result = runtime.run("researcher", "x" * 5000)

    assert result.status == "context_budget_exceeded"


def test_runtime_allows_a_bounded_recovery_step_override():
    model = ReplayModelClient([{
        "action": "activate_skill",
        "summary": "加载节点研究规范",
        "skill": "research-trace-node",
    }])
    runtime = AgentRuntime(
        model,
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=8,
    )

    result = runtime.run("researcher", "填写节点", max_steps=1)

    assert result.status == "step_limit"
    assert result.steps == 1


def test_agent_runtime_preserves_provider_rejection_as_terminal_status():
    calls = []

    def transport(*_args):
        calls.append(1)
        raise ModelRequestError("content rejected", error_code="provider_policy_rejected")

    model = _client(transport, max_retries=2)
    runtime = AgentRuntime(
        model,
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=2,
        max_model_errors=2,
    )

    result = runtime.run("researcher", "完成节点研究")

    assert result.status == "provider_rejected"
    assert len(calls) == 1


def test_conclusion_formatter_control_schema_has_only_its_current_action():
    runtime = AgentRuntime(
        ReplayModelClient([]),
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
    )

    before_activation = runtime._native_tools("conclusion_formatter", set())
    after_activation = runtime._native_tools(
        "conclusion_formatter", set(), active_skill="format-conclusion-ir",
    )

    assert len(before_activation) == len(after_activation) == 1
    assert before_activation[0]["function"]["parameters"]["properties"]["action"]["enum"] == ["activate_skill"]
    final_schema = after_activation[0]["function"]["parameters"]
    assert final_schema["properties"]["action"]["enum"] == ["final", "activate_skill"]
    assert final_schema["properties"]["answer"]["type"] == "object"
    assert final_schema["properties"]["answer"]["properties"]["conclusion_ir"]["properties"]["blocks"]["items"]["anyOf"]


def test_conclusion_formatter_accepts_structured_answer_object():
    payload = {
        "node_id": "node-1",
        "slot_id": "origin_early_signals",
        "revision": 0,
        "conclusion_ir": {
            "schema_version": "conclusion-ir.v1",
            "blocks": [{"type": "list", "ordered": True, "items": ["第一项", "第二项"]}],
        },
    }
    tools = AgentRuntime(
        ReplayModelClient([]),
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
    )._native_tools("conclusion_formatter", set(), active_skill="format-conclusion-ir")

    def transport(_base_url, _api_key, _request):
        return {"choices": [{
            "message": {"tool_calls": [{
                "id": "call-format",
                "type": "function",
                "function": {
                    "name": "agent_control",
                    "arguments": json.dumps({
                        "action": "final", "summary": "交付结构化结论",
                        "skill": None, "reference": None, "answer": payload,
                    }, ensure_ascii=False),
                },
            }]},
            "finish_reason": "tool_calls",
        }]}

    model = _client(transport)
    model.start_session()
    decision = model.decide([{"role": "user", "content": "format"}], tools=tools)

    assert decision.action == AgentAction.FINAL
    assert decision.answer == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def test_conclusion_formatter_structured_repair_preserves_typed_answer_schema():
    payload = {
        "node_id": "node-1",
        "slot_id": "origin_early_signals",
        "revision": 0,
        "conclusion_ir": {
            "schema_version": "conclusion-ir.v1",
            "blocks": [{"type": "paragraph", "text": "核心结论。"}],
        },
    }
    tools = AgentRuntime(
        ReplayModelClient([]),
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
    )._native_tools("conclusion_formatter", set(), active_skill="format-conclusion-ir")
    requests = []

    def transport(_base_url, _api_key, request):
        requests.append(request)
        if len(requests) == 1:
            arguments = "{\"action\":\"final\",\"summary\":\"交付\",\"answer\":"
            return {"choices": [{
                "message": {"tool_calls": [{
                    "id": "call-malformed",
                    "type": "function",
                    "function": {"name": "agent_control", "arguments": arguments},
                }]},
                "finish_reason": "tool_calls",
            }]}
        return {"choices": [{
            "message": {"content": json.dumps({
                "action": "final", "summary": "完成修复",
                "skill": None, "reference": None, "answer": payload,
            }, ensure_ascii=False)},
            "finish_reason": "stop",
        }]}

    model = _client(transport)
    model.start_session()
    decision = model.decide([{"role": "user", "content": "format"}], tools=tools)

    repair_schema = requests[1]["response_format"]["json_schema"]["schema"]
    assert repair_schema["properties"]["answer"]["type"] == "object"
    assert decision.action == AgentAction.FINAL
    assert json.loads(decision.answer)["conclusion_ir"]["blocks"][0]["text"] == "核心结论。"


def test_json_context_can_restrict_conclusion_formatter_actions():
    messages = AgentContextAssembler().build(
        role="conclusion_formatter",
        objective="format conclusion",
        skill_index=[],
        active_skill="format-conclusion-ir",
        skill_instructions="instructions",
        observations=[],
        allowed_actions=(AgentAction.FINAL,),
    )

    payload = json.loads(messages[1]["content"])
    assert payload["agent_decision_schema"]["properties"]["action"]["enum"] == ["final"]


def test_formatter_runtime_rejects_skill_reference_actions_after_activation():
    model = ReplayModelClient([
        {"action": "activate_skill", "summary": "加载结论编辑规范", "skill": "format-conclusion-ir"},
        {"action": "read_skill_reference", "summary": "读取参考文件", "reference": "example.md"},
        {"action": "final", "summary": "交付结构化结论", "answer": "{}"},
    ])
    runtime = AgentRuntime(
        model,
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=3,
    )

    result = runtime.run("conclusion_formatter", "整理节点结论")

    assert result.status == "completed"
    assert any(item.get("error_code") == "formatter_action_not_allowed" for item in result.observations)
    assert "read_skill_reference" not in json.loads(model.messages[1][1]["content"])["agent_decision_schema"]["properties"]["action"]["enum"]


