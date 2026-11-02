"""A small real Agent loop: model decisions, Skills, tools, and recovery."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from threading import Lock
from typing import Any, Callable, Iterable

from .context import AgentContextAssembler
from .contracts import (
    AgentAction,
    AgentDecisionBatch,
    DecisionMode,
    AgentResult,
    AgentTraceEvent,
    AgentTraceSink,
    AgentRuntimeError,
    ModelClient,
    ToolGateway,
    ToolSchemaNotLoadedError,
)
from .prompts import RESEARCHER_EVIDENCE_CONTRACT, RolePrompt, role_prompt
from .skill_catalog import AgentSkillCatalog
from .model_clients import ModelDeadlineExceeded
from .research_progress import (
    ResearchProgress,
    SEARCH_CAPABILITIES,
    contract_error_requires_research,
)

_CONTROL_ONLY_ROLES = frozenset({"orchestrator", "conclusion_formatter", "report_composer"})


def _control_allowed_actions(role: str, active_skill: str | None) -> tuple[AgentAction, ...]:
    """Return the actions a control-only role may issue in this turn.

    A model may repeat ``activate_skill`` after a contract repair message.  A
    repeated activation is harmless and should be idempotent; rejecting it as
    a contract error wastes a turn and can exhaust the bounded retry budget.
    """

    if role == "orchestrator":
        return (AgentAction.FINAL, AgentAction.ACTIVATE_SKILL)
    if active_skill:
        return (AgentAction.FINAL, AgentAction.ACTIVATE_SKILL)
    return (AgentAction.ACTIVATE_SKILL,)


def _conclusion_formatter_answer_schema() -> dict[str, Any]:
    string = {"type": "string"}
    blocks = [
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["heading"]},
                "level": {"type": "integer", "enum": [1, 2, 3]},
                "text": string,
            },
            "required": ["type", "level", "text"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"type": {"type": "string", "enum": ["paragraph"]}, "text": string},
            "required": ["type", "text"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["list"]},
                "ordered": {"type": "boolean"},
                "items": {"type": "array", "items": string},
            },
            "required": ["type", "ordered", "items"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["table"]},
                "title": {"type": ["string", "null"]},
                "columns": {"type": "array", "items": string},
                "rows": {"type": "array", "items": {"type": "array", "items": string}},
            },
            "required": ["type", "title", "columns", "rows"],
            "additionalProperties": False,
        },
    ]
    return {
        "type": "object",
        "properties": {
            "node_id": string,
            "slot_id": string,
            "revision": {"type": "integer"},
            "conclusion_ir": {
                "type": "object",
                "properties": {
                    "schema_version": {"type": "string", "enum": ["conclusion-ir.v1"]},
                    "blocks": {"type": "array", "items": {"anyOf": blocks}},
                },
                "required": ["schema_version", "blocks"],
                "additionalProperties": False,
            },
        },
        "required": ["node_id", "slot_id", "revision", "conclusion_ir"],
        "additionalProperties": False,
    }


class AgentRuntime:
    def __init__(
        self,
        model: ModelClient,
        tools: ToolGateway,
        skills: AgentSkillCatalog,
        *,
        context: AgentContextAssembler | None = None,
        max_steps: int = 24,
        prompt_registry: dict[str, RolePrompt] | None = None,
        max_model_errors: int = 2,
        trace_sink: AgentTraceSink | Callable[[dict[str, Any]], Any] | None = None,
        activation_sink: Callable[[dict[str, Any]], Any] | None = None,
        deadline: float | None = None,
        validate_answer: Callable[[str], str] | None = None,
        max_contract_retries: int = 2,
        active_check: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.skills = skills
        self.context = context or AgentContextAssembler()
        self.max_steps = max_steps
        self.prompt_registry = prompt_registry
        self.max_model_errors = max(0, int(max_model_errors))
        self.trace_sink = trace_sink
        self.activation_sink = activation_sink
        self.deadline = deadline
        self.validate_answer = validate_answer
        self.max_contract_retries = max(0, int(max_contract_retries))
        self.active_check = active_check

    def run(
        self,
        role: str,
        objective: str,
        *,
        tool_run: Any | None = None,
        allowed_capabilities: Iterable[str] | None = None,
        context_pack: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
        max_steps: int | None = None,
        research_progress_state: ResearchProgress | None = None,
    ) -> AgentResult:
        self.model.start_session()
        deadline_setter = getattr(self.model, "set_deadline", None)
        if callable(deadline_setter):
            deadline_setter(self.deadline)
        observations: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        active_skill: str | None = None
        skill_instructions: str | None = None
        skill_index = self.skills.index(role=role)
        loaded_capabilities: set[str] = set()
        emitted_loaded_capabilities: set[str] = set()
        # A model can otherwise spend its whole turn budget polling an empty
        # or unavailable store. Keep this guard at the Agent boundary so one
        # noisy Skill cannot exhaust the shared Tool Gateway budget for the
        # other parallel research tasks. The limit is per Agent run, not a
        # provider-specific special case.
        capability_call_counts: dict[tuple[str, str], int] = {}
        finalize_only_nonfinal_turns = (
            research_progress_state.finalize_only_nonfinal_turns
            if role == "researcher" and research_progress_state is not None else 0
        )
        self.tools.ensure_discovered()
        # Discovery happens once when the Agent run starts. The stable
        # directory is then referenced by version instead of being serialized
        # into every subsequent turn.
        allowed = frozenset(allowed_capabilities) if allowed_capabilities is not None else None
        startup_tool_context = self._tool_context(role, loaded_capabilities, allowed)
        tool_directory_version = startup_tool_context.get("catalog_version")
        role_contract = role_prompt(role, self.prompt_registry)
        model_name = str(getattr(self.model, "model_name", self.model.__class__.__name__))
        mode = DecisionMode(self.model.decision_mode)
        native_tools = mode == DecisionMode.NATIVE
        model_errors = 0
        contract_errors = 0
        research_progress = research_progress_state if role == "researcher" else None

        step_limit = self.max_steps if max_steps is None else max(1, int(max_steps))
        for step in range(1, step_limit + 1):
            self._check_active()
            if self.deadline is not None and time.monotonic() >= self.deadline:
                return AgentResult(
                    "timeout", "The Agent stopped because its task deadline was exceeded.",
                    step - 1, active_skill, tuple(trace),
                    self.context.bounded_observations(observations),
                )
            tool_context = startup_tool_context
            newly_loaded = loaded_capabilities - emitted_loaded_capabilities
            if newly_loaded:
                    tool_context = self._tool_context(role, loaded_capabilities, allowed)
            pack = context_pack() if callable(context_pack) else context_pack
            if role == "researcher" and isinstance(pack, dict):
                if research_progress is None:
                    research_progress = ResearchProgress.from_context(pack)
                pack = dict(pack)
                pack["research_progress"] = research_progress.to_dict()
            if callable(deadline_setter):
                deadline_setter(self.deadline)
            allowed_for_turn = (
                frozenset()
                if research_progress is not None and research_progress.finalize_only
                else allowed
            )
            turn_tool_context = (
                self._tool_context(role, loaded_capabilities, allowed_for_turn)
                if allowed_for_turn != allowed else tool_context
            )
            finalization_only = bool(research_progress and research_progress.finalize_only)
            if finalization_only:
                turn_tool_context = {
                    **turn_tool_context,
                    "core_tools": [],
                    "deferred_tools": [],
                    "loaded_tools": [],
                }
            system_instructions = role_contract.instructions
            if role == "researcher":
                system_instructions += "\n" + RESEARCHER_EVIDENCE_CONTRACT
            if finalization_only and role == "researcher":
                system_instructions += (
                    "\n当前 context_pack.research_progress.finalize_only=true：所有检索与来源工具已经撤下，"
                    "只允许立即调用 agent_control(action=final) 交付 NodeDraft。"
                    "force_finalize=true 表示运行保护提前停止；false 可能表示已主动 ready 或当前只允许修复交付结构。"
                    "两种情况都不得再搜索或提交 checkpoint；强制停止时要保留未证实边界，不得宣称质量通过。"
                )
            messages = self.context.build(
                role=role,
                objective=objective,
                skill_index=skill_index,
                core_tools=turn_tool_context["core_tools"],
                deferred_tools=turn_tool_context["deferred_tools"],
                loaded_tools=turn_tool_context.get("loaded_tools", []),
                tool_directory_version=tool_directory_version,
                include_tool_directory=(step == 1 or not native_tools),
                native_tools=native_tools,
                active_skill=active_skill,
                skill_instructions=skill_instructions,
                observations=observations,
                context_pack=pack,
                system_instructions=system_instructions,
                prompt_version=role_contract.version,
                allowed_actions=(
                    _control_allowed_actions(role, active_skill)
                ) if role in _CONTROL_ONLY_ROLES else None,
            )
            emitted_loaded_capabilities.update(loaded_capabilities)
            budget_emitted = False
            try:
                diagnostic_context = {"role": role, "step": step}
                if isinstance(pack, dict):
                    task_context = pack.get("task") if isinstance(pack.get("task"), dict) else {}
                    state_context = pack.get("state") if isinstance(pack.get("state"), dict) else {}
                    draft_context = state_context.get("current_draft") if isinstance(state_context.get("current_draft"), dict) else {}
                    for key, value in {
                        "run_id": pack.get("run_id"),
                        "task_id": task_context.get("execution_task_id") or task_context.get("task_id"),
                        "node_id": task_context.get("node_id") or draft_context.get("node_id"),
                        "slot_id": task_context.get("slot_id") or draft_context.get("slot_id"),
                        "revision": task_context.get("revision") if task_context.get("revision") is not None else draft_context.get("revision"),
                    }.items():
                        if value is not None:
                            diagnostic_context[key] = value
                diagnostic_setter = getattr(self.model, "set_diagnostic_context", None)
                if callable(diagnostic_setter):
                    diagnostic_setter(diagnostic_context)
                decision_or_batch = self._decide_model(
                    messages,
                    role,
                    loaded_capabilities,
                    allowed_for_turn,
                    mode,
                    finalization_only=finalization_only,
                    active_skill=active_skill,
                )
                self._emit_budget_report(trace, role, step)
                budget_emitted = True
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    raise ModelDeadlineExceeded()
                if isinstance(decision_or_batch, AgentDecisionBatch):
                    if mode != DecisionMode.NATIVE:
                        raise ValueError("parallel AgentDecision batches require native function calling")
                    decision_or_batch.validate()
                else:
                    decision_or_batch.validate()
            except Exception as exc:
                self._check_active()
                error_code = str(getattr(exc, "error_code", "model_response_error"))
                if not budget_emitted:
                    self._emit_budget_report(trace, role, step)
                model_errors += 1
                self._record_native_result({
                    "status": "error",
                    "error_code": error_code,
                    "summary": _safe_error(exc),
                })
                self._observe(observations, {
                        "type": "model_error",
                        "error_code": error_code,
                        "summary": _safe_error(exc),
                        "recovery": ["repair_structured_output", "retry_once", "request_human"],
                    })
                self._emit_trace(trace, {
                        **AgentTraceEvent(
                            step=step,
                            action="model_error",
                            status="model_error",
                            role=role,
                            model=model_name,
                            prompt_version=role_contract.version,
                            prompt_hash=self.context.prompt_hash(messages),
                            skill=active_skill,
                            skill_version=(self.skills.activation_metadata(active_skill)["version"] if active_skill else None),
                            error_code=error_code,
                        ).to_dict(),
                        "summary": _safe_error(exc),
                    })
                if getattr(exc, "retryable", True) is False:
                    terminal_status = _terminal_model_status(error_code)
                    return AgentResult(
                        terminal_status,
                        _safe_error(exc), step, active_skill, tuple(trace),
                        self.context.bounded_observations(observations),
                    )
                if model_errors > self.max_model_errors:
                    return AgentResult(
                        "model_error", _safe_error(exc), step, active_skill, tuple(trace),
                        self.context.bounded_observations(observations),
                    )
                continue
            self._check_active()
            if isinstance(decision_or_batch, AgentDecisionBatch):
                if role in _CONTROL_ONLY_ROLES:
                    return AgentResult(
                        "invalid_role_contract",
                        f"{role} cannot issue business tool batches.",
                        step,
                        active_skill,
                        tuple(trace),
                        self.context.bounded_observations(observations),
                    )
                if finalization_only:
                    finalize_only_nonfinal_turns += 1
                    if research_progress is not None:
                        research_progress.finalize_only_nonfinal_turns = finalize_only_nonfinal_turns
                    for decision in decision_or_batch.decisions:
                        self._record_native_result({
                            "status": "error",
                            "error_code": "research_finalize_only",
                            "capability": decision.capability,
                        })
                    self._observe(observations, {
                        "type": "tool_blocked",
                        "error_code": "research_finalize_only",
                        "required_action": "submit NodeDraft using agent_control(action=final)",
                        "summary": "定稿模式不接受任何工具调用",
                    })
                    self._emit_trace(trace, {
                        "role": role,
                        "step": step,
                        "action": "research_finalize_only_violation",
                        "status": "blocked",
                        "error_code": "research_finalize_only",
                        "summary": "定稿模式只允许最终交付",
                    })
                    if finalize_only_nonfinal_turns > 1:
                        return AgentResult(
                            "model_error",
                            "Researcher did not submit a NodeDraft after one finalization-only correction.",
                            step,
                            active_skill,
                            tuple(trace),
                            self.context.bounded_observations(observations),
                        )
                    continue
                for decision in decision_or_batch.decisions:
                    event = AgentTraceEvent(
                        step=step,
                        action=decision.action.value,
                        status="decided",
                        role=role,
                        model=model_name,
                        prompt_version=role_contract.version,
                        prompt_hash=self.context.prompt_hash(messages),
                        skill=active_skill,
                        skill_version=(self.skills.activation_metadata(active_skill)["version"] if active_skill else None),
                        capability=decision.capability,
                    )
                    self._emit_trace(trace, {**event.to_dict(), "summary": decision.summary})
                self._execute_parallel_tool_batch(
                    role,
                    decision_or_batch,
                    startup_tool_context=startup_tool_context,
                    loaded_capabilities=loaded_capabilities,
                    native_tools=native_tools,
                    tool_directory_version=tool_directory_version,
                    observations=observations,
                    tool_run=tool_run,
                    capability_call_counts=capability_call_counts,
                    research_progress=research_progress,
                )
                continue
            decision = decision_or_batch
            if role in _CONTROL_ONLY_ROLES:
                allowed_control_actions = _control_allowed_actions(role, active_skill)
                if decision.action not in allowed_control_actions:
                    expected_action = allowed_control_actions[0]
                    contract_errors += 1
                    error_code = {
                        "orchestrator": "orchestrator_action_not_allowed",
                        "conclusion_formatter": "formatter_action_not_allowed",
                        "report_composer": "report_composer_action_not_allowed",
                    }[role]
                    summary = (
                        f"结论格式化流程当前只允许 {expected_action.value}。"
                        if role == "conclusion_formatter"
                        else f"报告生成流程当前只允许 {expected_action.value}。"
                    )
                    if role == "orchestrator":
                        summary = f"节点编排流程当前只允许 {expected_action.value}。"
                    feedback = {
                        "type": "contract_error",
                        "error_code": error_code,
                        "required_action": expected_action.value,
                        "summary": summary,
                    }
                    self._record_native_result({"status": "error", **feedback})
                    self._observe(observations, feedback)
                    self._emit_trace(trace, {
                        "role": role,
                        "step": step,
                        "action": "contract_error",
                        "status": "retry" if contract_errors <= self.max_contract_retries else "failed",
                        "error_code": feedback["error_code"],
                        "summary": feedback["summary"],
                        "attempt": contract_errors,
                    })
                    if contract_errors > self.max_contract_retries:
                        return AgentResult(
                            "invalid_role_contract",
                            feedback["summary"],
                            step,
                            active_skill,
                            tuple(trace),
                            self.context.bounded_observations(observations),
                        )
                    continue
            event = AgentTraceEvent(
                step=step,
                action=decision.action.value,
                status="decided",
                role=role,
                model=model_name,
                prompt_version=role_contract.version,
                prompt_hash=self.context.prompt_hash(messages),
                skill=active_skill,
                skill_version=(self.skills.activation_metadata(active_skill)["version"] if active_skill else None),
                capability=decision.capability,
            )
            self._emit_trace(trace, {**event.to_dict(), "summary": decision.summary})

            if finalization_only and decision.action != AgentAction.FINAL:
                finalize_only_nonfinal_turns += 1
                if research_progress is not None:
                    research_progress.finalize_only_nonfinal_turns = finalize_only_nonfinal_turns
                self._observe(observations, {
                    "type": "tool_blocked" if decision.action == AgentAction.CALL_TOOL else "control_error",
                    "capability": decision.capability,
                    "error_code": "research_finalize_only",
                    "required_action": "submit NodeDraft using agent_control(action=final)",
                    "summary": "定稿模式只允许最终交付",
                })
                self._record_native_result({"status": "error", "error_code": "research_finalize_only"})
                self._emit_trace(trace, {
                    "role": role,
                    "step": step,
                    "action": "research_finalize_only_violation",
                    "status": "blocked",
                    "error_code": "research_finalize_only",
                    "summary": f"定稿模式拒绝非最终动作：{decision.action.value}",
                })
                if finalize_only_nonfinal_turns > 1:
                    return AgentResult(
                        "model_error",
                        "Researcher did not submit a NodeDraft after one finalization-only correction.",
                        step,
                        active_skill,
                        tuple(trace),
                        self.context.bounded_observations(observations),
                    )
                continue

            if decision.action == AgentAction.RESEARCH_CHECKPOINT:
                if role != "researcher" or research_progress is None:
                    self._observe(observations, {
                        "type": "control_error", "error_code": "checkpoint_not_allowed",
                        "summary": "research_checkpoint is only available to the researcher",
                    })
                    self._record_native_result({"status": "error", "error_code": "checkpoint_not_allowed"})
                    continue
                try:
                    checkpoint = json.loads(decision.answer or "")
                    if not isinstance(checkpoint, dict):
                        raise ValueError("checkpoint must be a JSON object")
                    accepted, checkpoint_result = research_progress.accept_checkpoint(checkpoint)
                    status = str(checkpoint_result.get("status") or "").strip().lower()
                    if not accepted:
                        self._observe(observations, {
                            "type": "control_error",
                            **checkpoint_result,
                        })
                        self._record_native_result({
                            "status": "error",
                            "error_code": checkpoint_result.get("error_code", "invalid_research_checkpoint"),
                        })
                        continue
                    if status == "continue":
                        self._observe(observations, {
                            "type": "research_checkpoint", "status": "continue",
                            "next_queries": [str(item)[:240] for item in checkpoint_result.get("next_queries", [])],
                            "covered_aspects": [str(item)[:160] for item in checkpoint_result.get("covered_aspects", [])],
                        })
                    else:
                        self._observe(observations, {
                            "type": "research_checkpoint", "status": "ready",
                            "reason": str(checkpoint.get("reason") or "")[:500],
                            "covered_aspects": [str(item)[:160] for item in checkpoint_result.get("covered_aspects", [])],
                            "missing_aspects": [str(item)[:160] for item in checkpoint_result.get("missing_aspects", [])],
                            "forced": bool(checkpoint_result.get("forced")),
                            "stop_reason": checkpoint_result.get("stop_reason"),
                        })
                    self._record_native_result({"status": "accepted", "action": decision.action.value, "checkpoint_status": status})
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self._observe(observations, {
                        "type": "control_error", "error_code": "invalid_research_checkpoint",
                        "summary": str(exc)[:500],
                    })
                    self._record_native_result({"status": "error", "error_code": "invalid_research_checkpoint"})
                continue

            if decision.action == AgentAction.ACTIVATE_SKILL:
                requested_skill = decision.skill or ""
                try:
                    skill_instructions = self.skills.activate(requested_skill, role=role)
                    active_skill = requested_skill
                    metadata = self.skills.activation_metadata(requested_skill)
                    self._observe(observations, {"type": "skill_activated", **metadata})
                    if self.activation_sink is not None:
                        try:
                            self.activation_sink({
                                "type": "skill_activated",
                                **metadata,
                                "reason_summary": decision.summary,
                                "step_no": step,
                                "agent": role,
                            })
                        except Exception as exc:
                            # Audit persistence is deliberately non-blocking;
                            # the loop remains authoritative and exposes the
                            # sink failure as a bounded observation.
                            self._observe(observations, {
                                "type": "audit_error",
                                "audit": "skill_activation",
                                "error_code": "audit_persistence_error",
                                "summary": _safe_error(exc),
                            })
                except Exception as exc:
                    self._observe(observations, {
                            "type": "skill_error",
                            "skill": requested_skill,
                            "error_code": "skill_unavailable",
                            "summary": _safe_error(exc),
                            "recovery": ["choose_another_skill", "continue_without_skill", "request_human"],
                        })
                self._record_native_result({"status": "accepted", "action": decision.action.value})
                continue

            if decision.action == AgentAction.READ_SKILL_REFERENCE:
                if active_skill is None:
                    self._observe(observations, {"type": "skill_error", "error_code": "no_active_skill"})
                    self._record_native_result({"status": "error", "error_code": "no_active_skill"})
                    continue
                reference_loaded = False
                try:
                    content = self.skills.read_reference(active_skill, decision.reference or "")
                    reference_loaded = True
                    self._observe(observations, {"type": "skill_reference", "skill": active_skill, "reference": decision.reference, "content": content})
                except Exception as exc:
                    self._observe(observations, {"type": "skill_error", "error_code": "reference_unavailable", "summary": _safe_error(exc)})
                self._record_native_result({
                    "status": "completed" if reference_loaded else "error",
                    "action": decision.action.value,
                })
                continue

            if decision.action == AgentAction.CALL_TOOL:
                try:
                    capability = decision.capability or ""
                    if (
                        research_progress is not None
                        and capability in SEARCH_CAPABILITIES
                        and not research_progress.admit_query(decision.arguments.get("query"))
                    ):
                        raise AgentRuntimeError(
                            "research query was already attempted; provide a new targeted query",
                            "repeated_research_query",
                        )
                    call_key = (capability, _arguments_fingerprint(decision.arguments))
                    count = capability_call_counts.get(call_key, 0)
                    if count >= 3:
                        raise RuntimeError(
                            f"bounded repeated-call limit reached for identical request: {capability}"
                        )
                    capability_call_counts[call_key] = count + 1
                    deferred_capabilities = {
                        str(item.get("capability")) for item in startup_tool_context.get("deferred_tools", [])
                    }
                    if decision.capability in deferred_capabilities and decision.capability not in loaded_capabilities:
                        raise ToolSchemaNotLoadedError(decision.capability or "")
                    output = self._call_tool(
                        role,
                        capability,
                        decision.arguments,
                        tool_run=tool_run,
                    )
                    if research_progress is not None:
                        research_progress.observe_tool(capability, decision.arguments, output)
                    if decision.capability == "search_tools":
                        for item in output.get("tools", []):
                            if isinstance(item, dict) and item.get("capability"):
                                loaded_capabilities.add(str(item["capability"]))
                        if native_tools:
                            output = _native_search_result(output, tool_directory_version)
                    self._observe(observations, {"type": "tool_result", "capability": decision.capability, "output": _project_tool_payload(output)})
                except Exception as exc:
                    error_code = str(getattr(exc, "error_code", "tool_execution_error"))
                    self._record_native_result({"status": "error", "error_code": error_code, "summary": _safe_error(exc)})
                    self._observe(observations, {
                            "type": "tool_error",
                            "capability": decision.capability,
                            "error_code": error_code,
                            "summary": _safe_error(exc),
                            "recovery": _recovery_hints(error_code),
                        })
                    # A provider budget is a durable, scoped limit rather
                    # than a transient search miss.  Continuing to expose
                    # the same exhausted capability lets a researcher loop
                    # until its task deadline.  Preserve the evidence already
                    # collected and move directly to best-effort delivery;
                    # ordinary provider failures remain recoverable.
                    if (
                        role == "researcher"
                        and research_progress is not None
                        and error_code in {"provider_budget_exceeded", "global_budget_exceeded"}
                    ):
                        research_progress.request_finalize("tool_provider_budget_exhausted", forced=True)
                        self._observe(observations, {
                            "type": "research_budget_exhausted",
                            "error_code": error_code,
                            "required_action": "submit the best evidence-grounded NodeDraft using existing sources",
                            "summary": "检索提供商预算已耗尽，保留已有证据并进入节点交付阶段",
                        })
                else:
                    self._record_native_result({"status": "succeeded", "capability": decision.capability, "output": output})
                continue

            answer = decision.answer or ""
            if decision.action == AgentAction.FINAL and self.validate_answer is not None:
                try:
                    answer = self.validate_answer(answer)
                except (ValueError, TypeError, KeyError) as exc:
                    contract_errors += 1
                    if research_progress is not None:
                        # Once a provider budget is exhausted, reopening
                        # retrieval for an evidence-shaped validation error
                        # creates an unproductive retry loop. Keep the
                        # finalization reserve active and let the bounded
                        # contract retry report the genuine evidence gap.
                        if (
                            contract_error_requires_research(exc)
                            and research_progress.stop_reason != "tool_provider_budget_exhausted"
                        ):
                            research_progress.reopen_for_research(
                                "contract_validation_requires_fresh_evidence"
                            )
                        elif not research_progress.finalize_only:
                            research_progress.request_finalize(
                                "structural_contract_repair", forced=False
                            )
                    feedback = {
                        "type": "contract_error", "status": "error",
                        "error_code": "invalid_role_contract", "summary": str(exc)[:1500],
                        "recovery": "修正当前草稿后重新提交；保留已有搜索材料，勿编造来源或用日志代替证据。",
                    }
                    self._record_native_result(feedback)
                    self._observe(observations, feedback)
                    self._emit_trace(trace, {
                        "role": role, "step": step, "action": "contract_error",
                        "status": "retry" if contract_errors <= self.max_contract_retries else "failed",
                        "error_code": "invalid_role_contract", "summary": feedback["summary"],
                        "attempt": contract_errors,
                    })
                    if contract_errors > self.max_contract_retries:
                        return AgentResult("invalid_role_contract", feedback["summary"], step, active_skill,
                                           tuple(trace), self.context.bounded_observations(observations))
                    continue
            status = "needs_human" if decision.action == AgentAction.REQUEST_HUMAN else "completed"
            self._record_native_result({"status": "accepted", "action": decision.action.value})
            return AgentResult(
                status,
                answer,
                step,
                active_skill,
                tuple(trace),
                self.context.bounded_observations(observations),
            )

        return AgentResult(
            "step_limit",
            "The Agent stopped because its step budget was exhausted.",
            step_limit,
            active_skill,
            tuple(trace),
            self.context.bounded_observations(observations),
        )

    def _check_active(self) -> None:
        if self.active_check is not None:
            self.active_check()

    def _emit_budget_report(self, trace: list[dict[str, Any]], role: str, step: int) -> None:
        report = getattr(self.model, "last_budget_report", None)
        if report is None:
            return
        payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        self._emit_trace(trace, {
            "step": step,
            "action": "context_budget",
            "status": "measured",
            "role": role,
            **payload,
        })
        compaction = getattr(self.model, "last_compaction", None)
        if compaction is not None:
            self._emit_trace(trace, {
                "step": step,
                "action": "context_compaction",
                "status": "applied" if compaction.get("applied") else "not_needed",
                "role": role,
                **compaction,
            })

    def _execute_parallel_tool_batch(
        self,
        role: str,
        batch: AgentDecisionBatch,
        *,
        startup_tool_context: dict[str, Any],
        loaded_capabilities: set[str],
        native_tools: bool,
        tool_directory_version: str | None,
        observations: list[dict[str, Any]],
        tool_run: Any | None,
        capability_call_counts: dict[tuple[str, str], int],
        research_progress: ResearchProgress | None = None,
    ) -> None:
        """Run independent native Tool calls concurrently and preserve call order.

        Provider responses identify Tool calls by an ordered list of IDs.  Tool
        execution can finish in any order, but the assistant transcript must
        append its corresponding ``role=tool`` messages in the provider order.
        """

        decisions = batch.decisions
        deferred_capabilities = {
            str(item.get("capability")) for item in startup_tool_context.get("deferred_tools", [])
        }
        call_count_lock = Lock()

        def invoke(decision):
            self._check_active()
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise ModelDeadlineExceeded()
            capability = decision.capability or ""
            call_key = (capability, _arguments_fingerprint(decision.arguments))
            with call_count_lock:
                if (
                    research_progress is not None
                    and capability in SEARCH_CAPABILITIES
                    and not research_progress.admit_query(decision.arguments.get("query"))
                ):
                    raise AgentRuntimeError(
                        "research query was already attempted; provide a new targeted query",
                        "repeated_research_query",
                    )
                count = capability_call_counts.get(call_key, 0)
                if count >= 3:
                    raise RuntimeError(
                        f"bounded repeated-call limit reached for identical request: {capability}"
                    )
                # Reserve the slot before entering the worker.  This prevents
                # a native response containing duplicate searches from
                # consuming the global Tool budget.
                capability_call_counts[call_key] = count + 1
            if decision.capability in deferred_capabilities and decision.capability not in loaded_capabilities:
                raise ToolSchemaNotLoadedError(decision.capability or "")
            return self._call_tool(role, decision.capability or "", decision.arguments, tool_run=tool_run)

        outcomes: list[tuple[bool, Any] | None] = [None] * len(decisions)
        configured_workers = int(getattr(self.model, "max_parallel_tool_calls", len(decisions)) or len(decisions))
        max_workers = max(1, min(len(decisions), configured_workers))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="echotrace-agent-tool") as executor:
            futures = {executor.submit(invoke, decision): index for index, decision in enumerate(decisions)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    outcomes[index] = (True, future.result())
                except Exception as exc:
                    outcomes[index] = (False, exc)

        self._check_active()
        for decision, outcome in zip(decisions, outcomes, strict=True):
            assert outcome is not None
            succeeded, value = outcome
            if succeeded:
                output = value
                if research_progress is not None:
                    research_progress.observe_tool(decision.capability or "", decision.arguments, output)
                if decision.capability == "search_tools":
                    for item in output.get("tools", []):
                        if isinstance(item, dict) and item.get("capability"):
                            loaded_capabilities.add(str(item["capability"]))
                    if native_tools:
                        output = _native_search_result(output, tool_directory_version)
                self._observe(observations, {"type": "tool_result", "capability": decision.capability, "output": _project_tool_payload(output)})
                self._record_native_result({"status": "succeeded", "capability": decision.capability, "output": output})
                continue
            error = value
            error_code = str(getattr(error, "error_code", "tool_execution_error"))
            self._record_native_result({"status": "error", "error_code": error_code, "summary": _safe_error(error)})
            self._observe(observations, {
                "type": "tool_error",
                "capability": decision.capability,
                "error_code": error_code,
                "summary": _safe_error(error),
                "recovery": _recovery_hints(error_code),
            })
            if (
                role == "researcher"
                and research_progress is not None
                and error_code in {"provider_budget_exceeded", "global_budget_exceeded"}
            ):
                research_progress.request_finalize("tool_provider_budget_exhausted", forced=True)
                self._observe(observations, {
                    "type": "research_budget_exhausted",
                    "error_code": error_code,
                    "required_action": "submit the best evidence-grounded NodeDraft using existing sources",
                    "summary": "检索提供商预算已耗尽，保留已有证据并进入节点交付阶段",
                })

    def _emit_trace(self, trace: list[dict[str, Any]], event: dict[str, Any]) -> None:
        trace.append(event)
        sink = self.trace_sink
        if sink is None:
            return
        try:
            writer = getattr(sink, "write", None)
            if callable(writer):
                writer(dict(event))
            elif callable(sink):
                sink(dict(event))
            else:
                raise TypeError("trace_sink must be callable or implement write(event)")
        except Exception:
            # Trace persistence must never change Agent semantics. The in-memory
            # result remains authoritative; callers can monitor sink failures.
            return

    def _observe(self, observations: list[dict[str, Any]], value: dict[str, Any]) -> None:
        """Store only bounded observations so long outputs cannot grow state."""

        observations.append(self.context._bounded(value))

    def _call_tool(
        self,
        role: str,
        capability: str,
        arguments: dict[str, Any],
        *,
        tool_run: Any | None,
    ) -> dict[str, Any]:
        """Pass an optional runtime trace port without constraining test gateways."""

        return self.tools.call(role, capability, arguments, run=tool_run)

    def _record_native_result(self, result: dict[str, Any]) -> None:
        try:
            # Provider output can contain full web pages or many search
            # documents. Keep the raw value in the Tool Gateway audit path,
            # but send a bounded, source-preserving projection to the model.
            # This prevents a single native tool result from exhausting the
            # context window or being rejected by provider data inspection.
            self.model.record_result(_model_visible_result(result))
        except Exception:
            return

    def _tool_context(
        self,
        role: str,
        loaded_capabilities: set[str],
        allowed_capabilities: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Build the model-visible directory from the Tool Gateway contract."""

        if role in _CONTROL_ONLY_ROLES:
            return {
                "core_tools": [],
                "deferred_tools": [],
                "loaded_tools": [],
                "catalog_version": f"control-only:{role}:v1",
            }
        return dict(self.tools.tool_context(
            role,
            capabilities=tuple(sorted(allowed_capabilities)) if allowed_capabilities is not None else None,
            loaded_capabilities=tuple(sorted(loaded_capabilities)),
        ))

    def _native_tools(
        self,
        role: str,
        loaded_capabilities: set[str],
        allowed_capabilities: frozenset[str] | None = None,
        *,
        finalization_only: bool = False,
        active_skill: str | None = None,
    ) -> list[dict[str, Any]]:
        functions = [] if role in _CONTROL_ONLY_ROLES else list(self.tools.native_tools(
            role,
            capabilities=tuple(sorted(allowed_capabilities)) if allowed_capabilities is not None else None,
            loaded_capabilities=tuple(sorted(loaded_capabilities)),
        ))
        from .protocol import control_function
        allowed_actions = None
        if role in _CONTROL_ONLY_ROLES:
            allowed_actions = _control_allowed_actions(role, active_skill)
        elif finalization_only:
            allowed_actions = (AgentAction.FINAL,)
        if finalization_only:
            functions = []
        answer_schema = (
            _conclusion_formatter_answer_schema()
            if role == "conclusion_formatter" and active_skill
            else None
        )
        functions.append(control_function(
            strict=True,
            allowed_actions=allowed_actions,
            answer_schema=answer_schema,
        ))
        return functions

    def _decide_model(
        self,
        messages: list[dict[str, str]],
        role: str,
        loaded_capabilities: set[str],
        allowed_capabilities: frozenset[str] | None = None,
        mode: DecisionMode = DecisionMode.JSON,
        *,
        finalization_only: bool = False,
        active_skill: str | None = None,
    ):
        decide = self.model.decide
        if mode == DecisionMode.NATIVE:
            tools = self._native_tools(
                role,
                loaded_capabilities,
                allowed_capabilities,
                finalization_only=finalization_only,
                active_skill=active_skill,
            )
            return decide(messages, tools=tools)
        return decide(messages)

def _safe_error(error: Exception) -> str:
    value = " ".join(str(error).split())
    return (value or error.__class__.__name__)[:240]


def _arguments_fingerprint(arguments: dict[str, Any]) -> str:
    return sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _native_search_result(output: dict[str, Any], catalog_version: str | None) -> dict[str, Any]:
    """Keep native tool results small; schemas are installed in the next tools list."""

    loaded: list[dict[str, Any]] = []
    for item in output.get("tools", []):
        if not isinstance(item, dict) or not item.get("capability"):
            continue
        contract = {
            "capability": item["capability"],
            "description": item.get("description", ""),
            "input_schema": item.get("input_schema", {}),
            "output_schema": item.get("output_schema", {}),
        }
        loaded.append({
            "capability": contract["capability"],
            "description": contract["description"],
            "schema_hash": sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            "status": "available_in_next_tools",
        })
    return {"loaded": loaded, "count": len(loaded), "catalog_version": catalog_version}


def _model_visible_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded representation used in a native tool transcript.

    Raw provider payloads remain available to the gateway audit and the
    durable source records. The model only needs enough metadata and excerpts
    to decide the next operation or construct a source-located artifact.
    """

    visible = dict(result)
    if "output" in visible:
        visible["output"] = _project_tool_payload(visible["output"])
    return visible


def _project_tool_payload(value: Any) -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"documents", "results", "items", "records"} and isinstance(item, list):
                projected[key] = _project_document_list(item)
            elif key in {"document", "record"} and isinstance(item, dict):
                projected[key] = _project_document(item, max_content_chars=6000)
            elif isinstance(item, (dict, list)):
                projected[key] = _project_tool_payload(item)
            elif isinstance(item, str) and len(item) > 2000:
                projected[key] = item[:2000] + "...[truncated]"
                projected[f"{key}_truncated"] = True
            else:
                projected[key] = item
        return projected
    if isinstance(value, list):
        return [_project_tool_payload(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 6000:
        return value[:6000] + "...[truncated]"
    return value


def _project_document_list(items: list[Any]) -> dict[str, Any]:
    visible = [
        _project_document(item, max_content_chars=2400)
        for item in items[:10]
        if isinstance(item, dict)
    ]
    return {
        "items": visible,
        "count": len(items),
        "omitted": max(0, len(items) - len(visible)),
        "content_note": "content is an excerpt for model context; use fetch_public_page for a known URL when more text is required",
    }


def _project_document(item: dict[str, Any], *, max_content_chars: int) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in ("document_id", "source", "title", "url", "published_at", "fetched_at", "author", "content_hash", "metadata"):
        if key in item:
            projected[key] = item[key]
    content = item.get("content") or item.get("raw_content") or item.get("snippet")
    if content is not None:
        text = str(content)
        projected["content"] = text[:max_content_chars] + ("...[truncated]" if len(text) > max_content_chars else "")
        projected["content_chars"] = len(text)
        projected["content_truncated"] = len(text) > max_content_chars
    for key, value in item.items():
        if key not in projected and key not in {"content", "raw_content", "snippet"}:
            if isinstance(value, (str, int, float, bool)) or value is None:
                projected[key] = value
    return projected


def _recovery_hints(error_code: str) -> list[str]:
    return {
        "timeout": ["retry_once", "narrow_request", "choose_alternative_tool"],
        "capability_unavailable": ["choose_alternative_tool", "state_evidence_gap"],
        "missing_configuration": ["request_human", "state_evidence_gap"],
        "permission_denied": ["request_human", "stop_disallowed_action"],
        "capability_denied": ["request_human", "stop_disallowed_action"],
        "contract_error": ["repair_arguments", "state_evidence_gap"],
    }.get(error_code, ["retry_once", "state_evidence_gap"])


def _terminal_model_status(error_code: str) -> str:
    """Keep provider policy refusals distinct from recoverable model errors.

    ``ModelRequestError`` marks transport and provider-policy failures as
    non-retryable at the model boundary.  The Agent result is consumed by the
    live runner, which must stop immediately for a provider refusal rather
    than handing it to the generic model-error/role retry path.
    """

    if error_code == "agent_task_timeout":
        return "timeout"
    if error_code == "provider_policy_rejected":
        return "provider_rejected"
    if error_code == "provider_context_length":
        return "context_budget_exceeded"
    if error_code == "provider_unavailable":
        return "provider_unavailable"
    if error_code == "context_budget_exceeded":
        return "context_budget_exceeded"
    return "model_error"
