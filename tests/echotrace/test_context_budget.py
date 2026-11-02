from __future__ import annotations

import json

from echotrace.agent.context_budget import (
    ProviderContextProfile,
    build_context_budget_report,
    estimate_tokens,
)
from echotrace.agent.context_pack import build_context_pack


def test_budget_profile_exposes_hard_input_limit():
    profile = ProviderContextProfile("model", 1000, 200, target_ratio=0.8)
    assert profile.input_limit_tokens == 800
    assert profile.role_target_tokens("researcher") == 640


def test_mixed_char_v2_uses_a_calibrated_cjk_rate_and_keeps_other_unicode_conservative():
    assert estimate_tokens("汉" * 1000) == 1100
    assert estimate_tokens("😀") == 2


def test_qwen_calibration_is_scoped_to_qwen_models():
    qwen = ProviderContextProfile.from_env("qwen3.8-flash", 1000, {})
    other = ProviderContextProfile.from_env("other-model", 1000, {})

    assert qwen.estimator == "mixed-char-v2"
    assert other.estimator == "mixed-char-v1"


def test_budget_report_attributes_structured_context_pack_layers():
    pack = build_context_pack("researcher", {
        "run_id": "run-1",
        "task": {"slot_id": "response", "revision": 1},
        "source_documents": [{"document_id": "doc-1", "title": "公告", "url": "https://example.test", "content": "正文"}],
    })
    messages = [{"role": "user", "content": json.dumps({"objective": "研究节点", "context_pack": pack}, ensure_ascii=False)}]
    report = build_context_budget_report(
        role="researcher", messages=messages, tools=[],
        profile=ProviderContextProfile("qwen-test", 10_000, 1_000, target_ratio=0.8),
    )

    assert report.context_pack_chars > 0
    assert report.context_pack_hash == pack["pack_hash"]
    assert report.context_field_chars["task"] > 0
    assert report.context_field_chars["evidence"] > 0


def test_budget_breakdown_counts_complete_assistant_tool_calls():
    assistant = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call-large",
            "type": "function",
            "function": {"name": "search_web", "arguments": json.dumps({"query": "武汉" * 500}, ensure_ascii=False)},
        }],
    }
    messages = [
        {"role": "system", "content": "system"},
        assistant,
        {"role": "tool", "tool_call_id": "call-large", "content": "{}"},
        {"role": "user", "content": "latest task"},
    ]

    report = build_context_budget_report(
        role="researcher", messages=messages, tools=[],
        profile=ProviderContextProfile("qwen-test", 10_000, 1_000),
    )

    assistant_text = json.dumps(assistant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert report.transcript_chars >= len(assistant_text)
    assert report.transcript_tokens_estimated >= estimate_tokens(assistant_text)
    assert report.input_chars > report.transcript_chars
