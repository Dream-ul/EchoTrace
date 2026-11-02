"""Bounded, structure-aware compaction for native tool transcripts.

The transcript is a protocol conversation, not a free-form string.  This
module only removes complete assistant/tool turns or replaces complete tool
results with JSON references.  It never slices JSON and never changes the
latest task snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping


def transcript_hash(messages: Iterable[Mapping[str, Any]]) -> str:
    payload = json.dumps(list(messages), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TranscriptCompaction:
    messages: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Turn:
    messages: tuple[dict[str, Any], ...]
    call_ids: frozenset[str]
    complete: bool
    has_tool_result: bool


def compact_transcript(
    messages: Iterable[Mapping[str, Any]],
    *,
    measure: Callable[[list[dict[str, Any]]], int],
    soft_limit_tokens: int,
    hard_limit_tokens: int,
    pending_call_ids: Iterable[str] = (),
    keep_recent_turns: int = 2,
    context_pack_hash: str | None = None,
) -> TranscriptCompaction:
    """Compact a transcript while preserving provider message invariants.

    ``measure`` must include all request components (for example tool
    schemas), so the decision to stop is based on the actual provider payload.
    The reducer first replaces tool results with references, then removes
    complete turns from oldest to newest if the hard limit still cannot be
    met. The current user snapshot and pending calls are never removed.
    """

    original = [dict(message) for message in messages]
    before_hash = transcript_hash(original)
    before_tokens = int(measure(original))
    pending = frozenset(str(item) for item in pending_call_ids)
    if before_tokens <= soft_limit_tokens:
        return TranscriptCompaction(tuple(original), {
            "compaction_id": None,
            "applied": False,
            "strategy": "none",
            "before_transcript_hash": before_hash,
            "after_transcript_hash": before_hash,
            "before_tokens": before_tokens,
            "after_tokens": before_tokens,
            "context_pack_hash": context_pack_hash,
            "kept_turns": _turn_count(original),
            "cleared_tool_results": 0,
            "removed_turns": 0,
            "pending_call_ids": sorted(pending),
            "result_refs": [],
        })

    prefix, turns, suffix = _split_transcript(original)
    keep_from = max(0, len(turns) - max(0, int(keep_recent_turns)))
    mutable_turns = [list(turn.messages) for turn in turns]
    cleared = 0
    result_refs: list[str] = []

    # Older complete turns retain their assistant call and tool-call IDs, but
    # the potentially large result body becomes a deterministic reference.
    for index, turn in enumerate(turns):
        if index >= keep_from or not turn.complete or turn.call_ids & pending:
            continue
        replaced: list[dict[str, Any]] = []
        for message in mutable_turns[index]:
            if message.get("role") != "tool":
                replaced.append(message)
                continue
            ref = _replace_tool_result_if_smaller(message)
            if ref is None:
                replaced.append(message)
                continue
            result_refs.append(ref["result_ref"])
            replaced.append({
                "role": "tool",
                "tool_call_id": message.get("tool_call_id"),
                "content": json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            })
            cleared += 1
        mutable_turns[index] = replaced

    candidate = _join(prefix, mutable_turns, suffix)
    strategy = "replace_tool_results"
    removed_turns = 0

    # A recent result is preferred, but a single oversized recent result must
    # still be projected before sending.  Apply the same safe replacement to
    # remaining complete turns when the soft budget is still exceeded.
    if measure(candidate) > soft_limit_tokens:
        for index, turn in enumerate(turns):
            if measure(candidate) <= soft_limit_tokens:
                break
            if not turn.complete or turn.call_ids & pending or index < keep_from:
                continue
            replaced: list[dict[str, Any]] = []
            changed_turn = False
            for message in mutable_turns[index]:
                if message.get("role") != "tool":
                    replaced.append(message)
                    continue
                ref = _replace_tool_result_if_smaller(message)
                if ref is None:
                    replaced.append(message)
                    continue
                result_refs.append(ref["result_ref"])
                replaced.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id"),
                    "content": json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                })
                cleared += 1
                changed_turn = True
            if changed_turn:
                mutable_turns[index] = replaced
        candidate = _join(prefix, mutable_turns, suffix)

    # If references and recent tool history are still too large, remove whole
    # completed turns. The latest user snapshot remains the source of truth.
    if measure(candidate) > hard_limit_tokens:
        for index, turn in enumerate(turns):
            candidate = _join(prefix, mutable_turns, suffix)
            if measure(candidate) <= hard_limit_tokens:
                break
            if not turn.complete or turn.call_ids & pending:
                continue
            if not mutable_turns[index]:
                continue
            mutable_turns[index] = []
            removed_turns += 1
        candidate = _join(prefix, mutable_turns, suffix)
        strategy = "replace_tool_results_and_remove_old_turns"

    after_tokens = int(measure(candidate))
    after_hash = transcript_hash(candidate)
    changed = candidate != original
    compaction_id = None
    if changed:
        seed = f"{before_hash}:{after_hash}:{cleared}:{removed_turns}".encode("utf-8")
        compaction_id = "compact_" + hashlib.sha256(seed).hexdigest()[:20]
    metadata = {
        "compaction_id": compaction_id,
        "applied": changed,
        "strategy": strategy if changed else "no_safe_reduction",
        "before_transcript_hash": before_hash,
        "after_transcript_hash": after_hash,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "context_pack_hash": context_pack_hash,
        "kept_turns": _turn_count(candidate),
        "cleared_tool_results": cleared,
        "removed_turns": removed_turns,
        "pending_call_ids": sorted(pending),
        "result_refs": result_refs,
        "requires_retrieval": bool(cleared or removed_turns),
        "soft_limit_tokens": int(soft_limit_tokens),
        "hard_limit_tokens": int(hard_limit_tokens),
    }
    return TranscriptCompaction(tuple(candidate), metadata)


def _split_transcript(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[_Turn], list[dict[str, Any]]]:
    """Split stable system/latest user messages from assistant/tool turns."""

    if not messages:
        return [], [], []
    prefix = [messages[0]]
    body = messages[1:]
    suffix: list[dict[str, Any]] = []
    if body and body[-1].get("role") == "user":
        suffix = [body.pop()]
    turns: list[_Turn] = []
    index = 0
    while index < len(body):
        message = body[index]
        if message.get("role") != "assistant":
            turns.append(_Turn((message,), frozenset(), True, False))
            index += 1
            continue
        calls = message.get("tool_calls") or []
        call_ids = frozenset(str(call.get("id")) for call in calls if isinstance(call, Mapping) and call.get("id"))
        group = [message]
        index += 1
        while index < len(body) and body[index].get("role") == "tool":
            group.append(body[index])
            index += 1
        result_ids = frozenset(str(item.get("tool_call_id")) for item in group[1:] if item.get("tool_call_id"))
        complete = not call_ids or call_ids == result_ids
        turns.append(_Turn(tuple(group), call_ids, complete, bool(result_ids)))
    return prefix, turns, suffix


def _join(prefix: list[dict[str, Any]], turns: list[list[dict[str, Any]]], suffix: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [*prefix, *(message for turn in turns for message in turn), *suffix]


def _replace_tool_result(message: Mapping[str, Any]) -> dict[str, Any]:
    content = message.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    status = "unknown"
    error_code = None
    keys: list[str] = []
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, Mapping):
        status = str(decoded.get("status") or "unknown")
        error_code = decoded.get("error_code")
        keys = sorted(str(key) for key in decoded.keys())[:16]
    result_ref = f"transcript:tool:{content_hash[:20]}"
    value: dict[str, Any] = {
        "status": status,
        "summary": "工具结果已压缩；需要时通过 result_ref 结合运行记录重新获取。",
        "result_ref": result_ref,
        "content_hash": f"sha256:{content_hash}",
        "keys": keys,
        "omitted": True,
    }
    if error_code:
        value["error_code"] = str(error_code)
    return value


def _replace_tool_result_if_smaller(message: Mapping[str, Any]) -> dict[str, Any] | None:
    content = message.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    replacement = _replace_tool_result(message)
    reference = json.dumps(replacement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return replacement if len(reference) < len(content) else None


def _turn_count(messages: list[dict[str, Any]]) -> int:
    return len(_split_transcript(messages)[1])


__all__ = ["TranscriptCompaction", "compact_transcript", "transcript_hash"]
