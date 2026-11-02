"""Provider-configured request sizing without changing prompt contents."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping


_CONTEXT_FIELDS = {
    "task": "task",
    "state": "state",
    "evidence": "evidence",
    "analysis": "analysis",
    "transcript": "transcript",
}


@dataclass(frozen=True, slots=True)
class ProviderContextProfile:
    model_name: str
    context_window_tokens: int
    output_reserve_tokens: int
    target_ratio: float = 0.9
    estimator: str = "mixed-char-v1"
    soft_ratio: float = 0.85
    hard_ratio: float = 0.95

    @classmethod
    def from_env(cls, model_name: str, output_reserve_tokens: int, environ: Mapping[str, str] | None = None) -> "ProviderContextProfile":
        env = environ or os.environ
        try:
            model_default_window = 1_000_000 if "qwen3.8-flash" in model_name.casefold() else 131_072
            window = int(env.get("ECHOTRACE_LLM_CONTEXT_WINDOW", str(model_default_window)))
            ratio = float(env.get("ECHOTRACE_CONTEXT_TARGET_RATIO", "0.9"))
            soft_ratio = float(env.get("ECHOTRACE_CONTEXT_SOFT_RATIO", "0.85"))
            hard_ratio = float(env.get("ECHOTRACE_CONTEXT_HARD_RATIO", "0.95"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("context profile values must be numeric") from exc
        if window < 1 or not 0.1 <= ratio <= 1.0 or not 0.1 <= soft_ratio <= 1.0 or not 0.1 <= hard_ratio <= 1.0:
            raise RuntimeError("context window and budget ratios must be valid")
        if soft_ratio > hard_ratio:
            raise RuntimeError("context soft ratio cannot exceed hard ratio")
        estimator = "mixed-char-v2" if "qwen" in model_name.casefold() else "mixed-char-v1"
        return cls(model_name, window, max(1, int(output_reserve_tokens)), ratio, estimator, soft_ratio, hard_ratio)

    @property
    def input_limit_tokens(self) -> int:
        return max(1, self.context_window_tokens - self.output_reserve_tokens)

    def role_target_tokens(self, role: str) -> int:
        configured = os.getenv(f"ECHOTRACE_CONTEXT_BUDGET_{role.upper()}", "").strip()
        if configured:
            try:
                return max(1, min(self.input_limit_tokens, int(configured)))
            except ValueError as exc:
                raise RuntimeError(f"ECHOTRACE_CONTEXT_BUDGET_{role.upper()} must be a positive integer") from exc
        return max(1, math.floor(self.input_limit_tokens * self.target_ratio))

    def soft_limit_tokens(self, role: str) -> int:
        configured = os.getenv(f"ECHOTRACE_CONTEXT_SOFT_{role.upper()}", "").strip()
        if configured:
            try:
                return max(1, min(self.input_limit_tokens, int(configured)))
            except ValueError as exc:
                raise RuntimeError(f"ECHOTRACE_CONTEXT_SOFT_{role.upper()} must be a positive integer") from exc
        return max(1, math.floor(self.input_limit_tokens * self.soft_ratio))

    def hard_limit_tokens(self, role: str) -> int:
        configured = os.getenv(f"ECHOTRACE_CONTEXT_HARD_{role.upper()}", "").strip()
        if configured:
            try:
                return max(1, min(self.input_limit_tokens, int(configured)))
            except ValueError as exc:
                raise RuntimeError(f"ECHOTRACE_CONTEXT_HARD_{role.upper()} must be a positive integer") from exc
        return max(1, math.floor(self.input_limit_tokens * self.hard_ratio))


@dataclass(frozen=True, slots=True)
class ContextBudgetReport:
    role: str
    model_name: str
    estimator: str
    input_chars: int
    input_tokens_estimated: int
    request_overhead_tokens_estimated: int
    context_window_tokens: int
    output_reserve_tokens: int
    target_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    provider_input_limit_tokens: int
    system_chars: int
    system_tokens_estimated: int
    user_chars: int
    user_tokens_estimated: int
    transcript_chars: int
    transcript_tokens_estimated: int
    tool_schema_chars: int
    tool_schema_tokens_estimated: int
    context_pack_chars: int
    context_pack_hash: str | None
    context_field_chars: dict[str, int]
    context_field_tokens: dict[str, int]
    context_pack_layer_stats: dict[str, dict[str, Any]]
    over_target: bool
    over_hard_limit: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_tokens(text: str, estimator: str = "mixed-char-v2") -> int:
    """Estimate multilingual request tokens without a vendor tokenizer.

    CJK-width characters are budgeted at 1.1 tokens each. Recent Qwen usage
    shows one token per common Han character on average; the extra 10% keeps
    room for vocabulary and formatting variation. Other Unicode stays at the
    conservative 1.5 rate, while ASCII retains the existing 3:1 estimate.
    """

    if estimator not in {"mixed-char-v1", "mixed-char-v2"}:
        raise ValueError(f"unsupported token estimator: {estimator}")
    if estimator == "mixed-char-v1":
        non_ascii = sum(1 for char in text if ord(char) > 127)
        ascii_chars = len(text) - non_ascii
        return max(0, math.ceil(non_ascii * 1.5 + ascii_chars / 3))

    ascii_chars = 0
    cjk_chars = 0
    other_unicode = 0
    for char in text:
        if char.isascii():
            ascii_chars += 1
        elif _is_cjk_width_character(char):
            cjk_chars += 1
        else:
            other_unicode += 1
    return max(0, math.ceil(cjk_chars * 1.1 + other_unicode * 1.5 + ascii_chars / 3))


def _is_cjk_width_character(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x2E80 <= codepoint <= 0x2EFF
        or 0x2F00 <= codepoint <= 0x2FDF
        or 0x2FF0 <= codepoint <= 0x2FFF
        or 0x3000 <= codepoint <= 0x30FF
        or 0x3100 <= codepoint <= 0x312F
        or 0x31A0 <= codepoint <= 0x31BF
        or 0x31C0 <= codepoint <= 0x33FF
        or 0x3400 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0xFE30 <= codepoint <= 0xFE4F
        or 0xFF01 <= codepoint <= 0xFF60
        or 0xFFE0 <= codepoint <= 0xFFE6
        or 0x20000 <= codepoint <= 0x323AF
    )


def build_context_budget_report(
    *,
    role: str,
    messages: Iterable[Mapping[str, Any]],
    tools: Iterable[Mapping[str, Any]] | None,
    profile: ProviderContextProfile,
) -> ContextBudgetReport:
    message_list = list(messages)
    tool_list = list(tools or ())
    input_payload = {"messages": message_list, "tools": tool_list}
    encoded = json.dumps(input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    by_role = {"system": 0, "user": 0, "transcript": 0}
    by_role_tokens = {"system": 0, "user": 0, "transcript": 0}
    context_pack_chars = 0
    context_pack_hash: str | None = None
    field_chars = {key: 0 for key in _CONTEXT_FIELDS}
    field_tokens = {key: 0 for key in _CONTEXT_FIELDS}
    context_pack_layers: dict[str, dict[str, Any]] = {}
    for message in message_list:
        message_text = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        message_role = str(message.get("role", ""))
        bucket = message_role if message_role in {"system", "user"} else "transcript"
        by_role[bucket] += len(message_text)
        by_role_tokens[bucket] += estimate_tokens(message_text, profile.estimator)
        if message_role != "user":
            continue
        try:
            content = message.get("content")
            content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
            user_payload = json.loads(content_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(user_payload, dict):
            continue
        pack = user_payload.get("context_pack")
        if not isinstance(pack, dict):
            continue
        context_pack_chars += len(json.dumps(pack, ensure_ascii=False, sort_keys=True, default=str))
        if context_pack_hash is None and isinstance(pack.get("pack_hash"), str):
            context_pack_hash = pack["pack_hash"]
        budget = pack.get("budget") if isinstance(pack.get("budget"), dict) else {}
        layers = budget.get("layers") if isinstance(budget.get("layers"), dict) else {}
        for name, stats in layers.items():
            if isinstance(stats, dict):
                context_pack_layers[str(name)] = dict(stats)
        for key in _CONTEXT_FIELDS:
            if key in pack:
                field_text = json.dumps(pack[key], ensure_ascii=False, sort_keys=True, default=str)
                field_chars[key] += len(field_text)
                field_tokens[key] += estimate_tokens(field_text, profile.estimator)

    input_tokens = estimate_tokens(encoded, profile.estimator)
    hard_limit = profile.hard_limit_tokens(role)
    soft_limit = profile.soft_limit_tokens(role)
    target = min(profile.role_target_tokens(role), hard_limit)
    tool_chars = len(json.dumps(tool_list, ensure_ascii=False, sort_keys=True, default=str))
    tool_tokens = estimate_tokens(json.dumps(tool_list, ensure_ascii=False, sort_keys=True, default=str), profile.estimator)
    request_overhead_tokens = input_tokens - sum(by_role_tokens.values()) - tool_tokens
    return ContextBudgetReport(
        role=role,
        model_name=profile.model_name,
        estimator=profile.estimator,
        input_chars=len(encoded),
        input_tokens_estimated=input_tokens,
        request_overhead_tokens_estimated=request_overhead_tokens,
        context_window_tokens=profile.context_window_tokens,
        output_reserve_tokens=profile.output_reserve_tokens,
        target_tokens=target,
        soft_limit_tokens=soft_limit,
        hard_limit_tokens=hard_limit,
        provider_input_limit_tokens=profile.input_limit_tokens,
        system_chars=by_role["system"],
        system_tokens_estimated=by_role_tokens["system"],
        user_chars=by_role["user"],
        user_tokens_estimated=by_role_tokens["user"],
        transcript_chars=by_role["transcript"],
        transcript_tokens_estimated=by_role_tokens["transcript"],
        tool_schema_chars=tool_chars,
        tool_schema_tokens_estimated=tool_tokens,
        context_pack_chars=context_pack_chars,
        context_pack_hash=context_pack_hash,
        context_field_chars=field_chars,
        context_field_tokens=field_tokens,
        context_pack_layer_stats=context_pack_layers,
        over_target=input_tokens > soft_limit,
        over_hard_limit=input_tokens > hard_limit,
    )


__all__ = ["ContextBudgetReport", "ProviderContextProfile", "build_context_budget_report", "estimate_tokens"]
