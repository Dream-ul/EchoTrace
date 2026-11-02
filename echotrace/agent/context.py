"""Bounded, explicit prompt assembly for an EchoTrace Agent turn."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Iterable


class AgentContextAssembler:
    def __init__(
        self,
        *,
        max_observations: int = 8,
        max_observation_chars: int = 4_000,
        # Keep the default aligned with AgentSkillCatalog's 64 KiB package
        # limit so activation injects the complete procedure. Callers with a
        # stricter model budget can lower this explicitly.
        max_skill_instruction_chars: int = 64 * 1024,
    ) -> None:
        self.max_observations = max_observations
        self.max_observation_chars = max_observation_chars
        self.max_skill_instruction_chars = max_skill_instruction_chars

    def build(
        self,
        *,
        role: str,
        objective: str,
        skill_index: Iterable[dict[str, str]],
        core_tools: Iterable[dict[str, Any]] | None = None,
        deferred_tools: Iterable[dict[str, Any]] | None = None,
        loaded_tools: Iterable[dict[str, Any]] | None = None,
        tool_directory_version: str | None = None,
        include_tool_directory: bool = True,
        native_tools: bool = False,
        active_skill: str | None,
        skill_instructions: str | None,
        observations: list[dict[str, Any]],
        context_pack: dict[str, Any] | None = None,
        system_instructions: str | None = None,
        prompt_version: str = "context.v1",
        allowed_actions: Iterable[Any] | None = None,
    ) -> list[dict[str, str]]:
        from .contracts import DecisionMode
        from .protocol import COMMON_INSTRUCTIONS, MODE_INSTRUCTIONS, PROTOCOL_VERSION, decision_schema
        mode = DecisionMode.NATIVE if native_tools else DecisionMode.JSON
        system = f"You are the EchoTrace {role}. {COMMON_INSTRUCTIONS} {MODE_INSTRUCTIONS[mode]}"
        if system_instructions:
            system = f"{system} {system_instructions}"
        payload = {
            "objective": objective,
            "prompt_version": prompt_version,
            "decision_protocol": PROTOCOL_VERSION,
            "skill_index": list(skill_index),
            "active_skill": active_skill,
            "skill_instructions": self._bounded_text(skill_instructions),
            "recent_observations": [self._bounded(item) for item in observations[-self.max_observations :]],
        }
        if context_pack is not None:
            payload["context_pack"] = self._bounded_context_pack(context_pack)
        if not native_tools:
            payload["agent_decision_schema"] = decision_schema(allowed_actions=allowed_actions)
        if include_tool_directory and not native_tools:
            payload["core_tools"] = list(core_tools or ())
            payload["deferred_tools"] = list(deferred_tools or ())
            payload["loaded_tools"] = list(loaded_tools or ())
            payload["tool_directory"] = {"status": "loaded", "version": tool_directory_version}
        elif native_tools:
            # Native function calling carries complete schemas in the API
            # ``tools`` field. Keeping them in the user message would duplicate
            # tokens and create two potentially divergent contracts. The short
            # deferred directory remains in each complete state snapshot, so
            # replacing an old snapshot does not hide available capabilities.
            payload["tool_directory"] = {"status": "native", "version": tool_directory_version}
            if deferred_tools:
                payload["deferred_tools"] = [
                    {
                        "capability": item.get("capability"),
                        "description": item.get("description", ""),
                        "use_when": item.get("use_when", []),
                        "avoid_when": item.get("avoid_when", []),
                    }
                    for item in deferred_tools
                ]
        else:
            payload["tool_directory"] = {"status": "unchanged", "version": tool_directory_version}
            if loaded_tools and not native_tools:
                payload["loaded_tools"] = list(loaded_tools)
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
        ]

    @staticmethod
    def prompt_hash(messages: Iterable[dict[str, str]]) -> str:
        """Hash a prompt snapshot without retaining the prompt in a trace."""

        encoded = json.dumps(list(messages), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return sha256(encoded).hexdigest()

    def bounded_observations(self, observations: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
        """Return a bounded snapshot suitable for a result or persistence sink."""

        return tuple(self._bounded(item) for item in list(observations)[-self.max_observations :])

    def _bounded(self, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= self.max_observation_chars:
            return value
        return {
            "type": value.get("type", "observation"),
            "truncated": True,
            "summary": encoded[: self.max_observation_chars],
        }

    def _bounded_text(self, value: str | None) -> str | None:
        if value is None or len(value) <= self.max_skill_instruction_chars:
            return value
        return value[: self.max_skill_instruction_chars] + "\n[skill instructions truncated]"

    @staticmethod
    def _bounded_context_pack(value: dict[str, Any]) -> dict[str, Any]:
        """Keep pack metadata intact while rejecting accidental unbounded input."""

        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        # Qwen3.8-Flash accepts a million-token context.  The role-aware pack
        # and token-budget reducer perform the normal compaction; this guard is
        # only a final protection against an accidentally unbounded payload.
        # Keep it well above ordinary packs so it does not become a hidden
        # low ceiling for source-grounded research.
        if len(encoded) <= 800_000:
            return value
        # ContextPack reducers should normally enforce this boundary.  The
        # final guard preserves pack identity and tells the Agent to reacquire
        # omitted resources instead of sending an unbounded request.
        return {
            "pack_version": value.get("pack_version"),
            "pack_hash": value.get("pack_hash"),
            "role": value.get("role"),
            "run_id": value.get("run_id"),
            "task": value.get("task", {}),
            "state": {"status": "omitted_after_pack_limit"},
            "evidence": [],
            "analysis": [],
            "transcript": [],
            "omissions": [{"kind": "context_pack", "reason": "assembler_limit", "reacquire": "role_context_builder"}],
            "budget": value.get("budget", {}),
        }
