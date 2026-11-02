from __future__ import annotations

import json

from echotrace.agent.context_budget import estimate_tokens
from echotrace.agent.transcript import compact_transcript


def _measure(messages):
    return estimate_tokens(json.dumps({"messages": messages}, ensure_ascii=False, sort_keys=True))


def _turn(call_id: str, result: str):
    return [
        {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def test_compaction_replaces_old_tool_results_without_breaking_pairs():
    messages = [
        {"role": "system", "content": "system"},
        *_turn("call-1", json.dumps({"status": "succeeded", "output": {"content": "x" * 5000}})),
        *_turn("call-2", json.dumps({"status": "succeeded", "output": {"content": "y" * 5000}})),
        {"role": "user", "content": "latest snapshot"},
    ]
    result = compact_transcript(
        messages,
        measure=_measure,
        soft_limit_tokens=100,
        hard_limit_tokens=1000,
        keep_recent_turns=1,
    )

    assert result.metadata["applied"] is True
    assert result.metadata["cleared_tool_results"] >= 1
    assert result.metadata["compaction_id"].startswith("compact_")
    compacted = list(result.messages)
    tool_messages = [item for item in compacted if item.get("role") == "tool"]
    assert len(tool_messages) == 2
    ref = json.loads(tool_messages[0]["content"])
    assert ref["omitted"] is True
    assert ref["result_ref"].startswith("transcript:tool:")
    assert [item["role"] for item in compacted] == ["system", "assistant", "tool", "assistant", "tool", "user"]


def test_compaction_never_removes_pending_call_turn():
    messages = [
        {"role": "system", "content": "system"},
        *_turn("call-1", "z" * 5000),
        {"role": "assistant", "tool_calls": [{"id": "call-pending", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "user", "content": "latest snapshot"},
    ]
    result = compact_transcript(
        messages,
        measure=_measure,
        soft_limit_tokens=100,
        hard_limit_tokens=150,
        pending_call_ids=["call-pending"],
        keep_recent_turns=0,
    )

    assert any(item.get("tool_calls", [{}])[0].get("id") == "call-pending" for item in result.messages if item.get("role") == "assistant")
    assert result.metadata["pending_call_ids"] == ["call-pending"]


def test_hard_limit_can_remove_a_recent_complete_turn_with_large_tool_arguments():
    def large_turn(call_id: str):
        return [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "arguments": json.dumps({"query": "武汉" * 500}, ensure_ascii=False),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "{}"},
        ]

    prefix = [{"role": "system", "content": "system"}]
    suffix = [{"role": "user", "content": "latest task snapshot"}]
    recent_turn = large_turn("call-2")
    messages = [*prefix, *large_turn("call-1"), *recent_turn, *suffix]
    hard_limit = _measure([*prefix, *recent_turn, *suffix])

    result = compact_transcript(
        messages,
        measure=_measure,
        soft_limit_tokens=hard_limit,
        hard_limit_tokens=hard_limit,
        keep_recent_turns=2,
    )

    compacted = list(result.messages)
    assert _measure(compacted) <= hard_limit
    assert "call-1" not in {item.get("tool_call_id") for item in compacted}
    assert "call-2" in {item.get("tool_call_id") for item in compacted}
    assert compacted[-1] == suffix[0]
    assert result.metadata["removed_turns"] == 1


def test_compaction_records_no_change_below_soft_limit():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "small"}]
    result = compact_transcript(messages, measure=_measure, soft_limit_tokens=100, hard_limit_tokens=200)
    assert result.metadata["applied"] is False
    assert result.metadata["compaction_id"] is None
    assert result.metadata["before_transcript_hash"] == result.metadata["after_transcript_hash"]
