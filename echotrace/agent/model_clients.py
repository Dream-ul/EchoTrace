"""Model adapters for live clients and deterministic replay fixtures.

The Agent kernel only depends on :class:`ModelClient`.  These adapters keep
model-specific SDKs outside the loop and make prompt/decision behavior
reproducible in CI without API credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
import json
import time
from hashlib import sha256
from typing import Any
from uuid import uuid4

from .contracts import AgentAction, AgentDecision, AgentDecisionBatch, DecisionMode
from .protocol import CONTROL_FUNCTION, decision_schema
from .context_budget import ContextBudgetReport, ProviderContextProfile, build_context_budget_report
from .context_pack import compact_context_pack
from .transcript import compact_transcript
from .diagnostics import JsonlDiagnostics


class ModelResponseError(ValueError):
    """Raised when a model response cannot be converted to an AgentDecision."""

    retryable = True

    def __init__(self, message: str, *, error_code: str = "model_response_error") -> None:
        super().__init__(message)
        self.error_code = error_code


class ModelRequestError(ModelResponseError):
    """A terminal request failure; transport retries have already been handled."""

    retryable = False

    def __init__(self, message: str, *, error_code: str = "model_request_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


class ModelDeadlineExceeded(ModelRequestError):
    def __init__(self) -> None:
        super().__init__("model request deadline exceeded", error_code="agent_task_timeout")


class ReplayModelClient:
    """Replay a finite sequence of recorded model decisions.

    Entries can be :class:`AgentDecision`, dictionaries, or JSON objects.  The
    received prompt snapshots are retained in ``messages`` for evals and can
    be hashed by callers without coupling the kernel to a provider SDK.
    """

    def __init__(
        self,
        responses: Iterable[AgentDecision | dict[str, Any] | str],
        *,
        model_name: str = "replay-model",
    ) -> None:
        self.model_name = model_name
        self._responses = list(responses)
        self.messages: list[Sequence[dict[str, str]]] = []

    @property
    def decision_mode(self) -> DecisionMode:
        return DecisionMode.JSON

    def start_session(self) -> None:
        return None

    def record_result(self, result: dict[str, Any]) -> None:
        return None

    def decide(self, messages: Sequence[dict[str, str]], *, tools: Sequence[dict[str, Any]] | None = None) -> AgentDecision:
        self.messages.append(messages)
        if not self._responses:
            raise ModelResponseError("recorded model responses exhausted")
        return parse_agent_decision(self._responses.pop(0))


class CallableModelClient:
    """Adapt a provider-specific callable returning structured JSON."""

    def __init__(self, call: Callable[[Sequence[dict[str, str]]], Any], *, model_name: str) -> None:
        self.call = call
        self.model_name = model_name
        self.messages: list[Sequence[dict[str, str]]] = []

    @property
    def decision_mode(self) -> DecisionMode:
        return DecisionMode.JSON

    def start_session(self) -> None:
        return None

    def record_result(self, result: dict[str, Any]) -> None:
        return None

    def decide(self, messages: Sequence[dict[str, str]], *, tools: Sequence[dict[str, Any]] | None = None) -> AgentDecision:
        self.messages.append(messages)
        return parse_agent_decision(self.call(messages))


class OpenAICompatibleModelClient:
    """Minimal OpenAI-compatible adapter with injectable transport.

    The default transport imports the OpenAI SDK only when a live call is
    requested. Tests can inject a transport and remain completely offline.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        transport: Callable[[str, str, dict[str, Any]], Any] | None = None,
        transport_guard: Callable[[str, str, dict[str, Any], Callable[[], Any], float], Any] | None = None,
        max_tokens: int = 32768,
        max_retry_tokens: int | None = None,
        timeout_seconds: float = 1200,
        decision_mode: DecisionMode = DecisionMode.NATIVE,
        max_parallel_tool_calls: int = 4,
        structured_fallback: bool = True,
        max_retries: int = 2,
        extra_body: dict[str, Any] | None = None,
        deadline: float | None = None,
        budget_role: str = "agent",
        diagnostics: JsonlDiagnostics | None = None,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> None:
        if not base_url.strip() or not api_key.strip() or not model_name.strip():
            raise ValueError("base_url, api_key and model_name are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.transport = transport or self._default_transport
        self.transport_guard = transport_guard
        self.max_tokens = max(1, int(max_tokens))
        self.max_retry_tokens = max(
            self.max_tokens,
            int(max_retry_tokens) if max_retry_tokens is not None else min(self.max_tokens * 2, 65536),
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._decision_mode = DecisionMode(decision_mode)
        self.max_parallel_tool_calls = max(1, int(max_parallel_tool_calls))
        self.structured_fallback = bool(structured_fallback)
        self.max_retries = max(0, int(max_retries))
        self.extra_body = dict(extra_body) if extra_body else None
        self.deadline = deadline
        self.budget_role = budget_role
        self.diagnostics = JsonlDiagnostics.from_env() if diagnostics is None else diagnostics
        self.diagnostic_context = dict(diagnostic_context or {})
        self.messages: list[Sequence[dict[str, str]]] = []
        self.last_usage: dict[str, int] = {}
        self.last_budget_report: ContextBudgetReport | None = None
        self.last_compaction: dict[str, Any] | None = None
        self.last_provider_diagnostics: dict[str, Any] = {}
        self._session_messages: list[dict[str, Any]] = []
        self._pending_native_message: dict[str, Any] | None = None
        self._pending_native_call_ids: list[str] = []
        self._native_assistant_recorded = False
        # Provider content inspection recovery is scoped to one Agent session.
        # Once raw source text has been refused, future state snapshots must
        # use the safe projection too; otherwise the next turn recreates the
        # same rejected payload from the durable ContextPack.
        self._provider_source_projection: str | None = None
        self.last_compaction = None

    @property
    def decision_mode(self) -> DecisionMode:
        return self._decision_mode

    def start_session(self) -> None:
        """Reset the provider conversation before a new Agent run."""

        self._session_messages = []
        self._pending_native_message = None
        self._pending_native_call_ids = []
        self._native_assistant_recorded = False
        self._provider_source_projection = None
        self.last_compaction = None

    def set_deadline(self, deadline: float | None) -> None:
        """Set an absolute monotonic deadline for the current Agent run."""
        self.deadline = deadline

    def set_diagnostic_context(self, context: dict[str, Any] | None) -> None:
        """Set low-sensitivity run/node fields attached to file diagnostics."""

        self.diagnostic_context.update(context or {})

    def decide(self, messages: Sequence[dict[str, Any]], *, tools: Sequence[dict[str, Any]] = ()) -> AgentDecision | AgentDecisionBatch:
        self.messages.append(messages)
        current = list(messages)
        if not current:
            raise ModelResponseError("model prompt must contain at least one message")
        # AgentContextAssembler sends a complete state snapshot on every turn.
        # Keeping every snapshot in the provider conversation multiplies the
        # node's source documents, draft and review feedback until the request
        # exceeds the provider input limit.  The durable native transcript is
        # the assistant/tool exchange; only the latest state snapshot belongs
        # beside it.  This also keeps JSON mode bounded when a model error
        # causes another decision without a tool result.
        latest_system = current[0]
        latest_user = current[-1]
        if not self._session_messages:
            initial_transcript = [
                message for message in current[1:-1]
                if str(message.get("role", "")) != "user"
            ]
            self._session_messages = [latest_system, *initial_transcript, latest_user]
        else:
            # Remove prior state snapshots but preserve every assistant/tool
            # pair.  A tool result is always preceded by its assistant call;
            # retaining the pair gives the model the complete search history
            # without replaying the large user payload.
            transcript = [
                message for message in self._session_messages[1:]
                if str(message.get("role", "")) != "user"
            ]
            self._session_messages = [latest_system, *transcript, latest_user]
        if self._provider_source_projection == "sanitized":
            self._session_messages = _sanitize_provider_payload({"messages": self._session_messages})["messages"]
        elif self._provider_source_projection == "stripped":
            self._session_messages = _strip_provider_source_context({"messages": self._session_messages})["messages"]
        request_tools = list(tools) if self._decision_mode == DecisionMode.NATIVE else []
        profile = ProviderContextProfile.from_env(self.model_name, self.max_tokens)
        self.last_budget_report = build_context_budget_report(
            role=self.budget_role,
            messages=self._session_messages,
            tools=request_tools,
            profile=profile,
        )
        self.last_compaction = None
        if self.last_budget_report.over_target:
            initial_report = self.last_budget_report
            context_pack_levels: list[dict[str, Any]] = []
            for level in range(1, 6):
                reduced = _compact_context_pack_messages(self._session_messages, level)
                if reduced is None:
                    continue
                candidate, reduction_metadata = reduced
                candidate_report = build_context_budget_report(
                    role=self.budget_role,
                    messages=candidate,
                    tools=request_tools,
                    profile=profile,
                )
                if candidate_report.input_tokens_estimated >= self.last_budget_report.input_tokens_estimated:
                    continue
                self._session_messages = candidate
                self.last_budget_report = candidate_report
                context_pack_levels.append({
                    **reduction_metadata,
                    "level": level,
                    "input_tokens_estimated": candidate_report.input_tokens_estimated,
                    "context_pack_hash": candidate_report.context_pack_hash,
                })
                if not candidate_report.over_target:
                    break

            def measure(candidate: list[dict[str, Any]]) -> int:
                return build_context_budget_report(
                    role=self.budget_role,
                    messages=candidate,
                    tools=request_tools,
                    profile=profile,
                ).input_tokens_estimated

            compacted = compact_transcript(
                self._session_messages,
                measure=measure,
                soft_limit_tokens=self.last_budget_report.soft_limit_tokens,
                hard_limit_tokens=self.last_budget_report.hard_limit_tokens,
                pending_call_ids=self._pending_native_call_ids,
                context_pack_hash=self.last_budget_report.context_pack_hash,
            )
            self._session_messages = [dict(message) for message in compacted.messages]
            self.last_compaction = compacted.metadata
            self.last_budget_report = build_context_budget_report(
                role=self.budget_role,
                messages=self._session_messages,
                tools=request_tools,
                profile=profile,
            )
            if context_pack_levels:
                transcript_metadata = dict(compacted.metadata)
                pack_metadata = {
                    "applied": True,
                    "strategy": "role_aware_context_pack_reduction",
                    "levels": context_pack_levels,
                    "before_context_pack_hash": initial_report.context_pack_hash,
                    "after_context_pack_hash": self.last_budget_report.context_pack_hash,
                    "before_resources": context_pack_levels[0].get("before_resources"),
                    "after_resources": context_pack_levels[-1].get("after_resources"),
                }
                transcript_metadata.update({
                    "compaction_id": transcript_metadata.get("compaction_id") or _compaction_id(
                        initial_report.context_pack_hash,
                        self.last_budget_report.context_pack_hash,
                    ),
                    "applied": True,
                    "strategy": (
                        "context_pack+" + transcript_metadata["strategy"]
                        if transcript_metadata.get("applied") else "context_pack_reduction"
                    ),
                    "before_tokens": initial_report.input_tokens_estimated,
                    "after_tokens": self.last_budget_report.input_tokens_estimated,
                    "context_pack_hash": self.last_budget_report.context_pack_hash,
                    "context_pack_compaction": pack_metadata,
                })
                self.last_compaction = transcript_metadata
            else:
                self.last_compaction = dict(compacted.metadata)
            if self.last_budget_report.over_hard_limit:
                self._log_local_error(
                    self._session_messages,
                    error_code="context_budget_exceeded",
                    message="context remains above the provider hard input budget after ContextPack and transcript compaction",
                    tools=request_tools,
                )
                raise ModelRequestError(
                    "context remains above the provider hard input budget after ContextPack and transcript compaction",
                    error_code="context_budget_exceeded",
                )
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": list(self._session_messages),
            "temperature": 0,
            "max_tokens": self.max_tokens,
            **({
                "tools": request_tools,
                "tool_choice": "auto",
                # The runtime understands a bounded batch of native
                # read-only Tool calls and writes each result back in
                # the standard assistant/tool transcript order.
                "parallel_tool_calls": True,
            } if request_tools else {}),
        }
        if self.extra_body:
            # Deployment-gated provider extensions (for example disabling
            # thinking mode on a gateway that spends the output budget on
            # reasoning before emitting tool calls). Never part of the
            # Agent contract; purely transport-level.
            payload["extra_body"] = self.extra_body
        response = self._request(payload)
        self.last_usage = _extract_usage(response)
        message = _response_message(response)
        refusal = message.get("refusal") if isinstance(message, dict) else getattr(message, "refusal", None)
        if refusal or _choice_field(response, "finish_reason") == "content_filter":
            raise ModelRequestError("model provider refused the response", error_code="provider_policy_rejected")
        calls = _message_tool_calls(message)
        if self._decision_mode == DecisionMode.JSON and calls:
            raise ModelResponseError("native tool calls are not allowed in JSON mode")
        if self._decision_mode == DecisionMode.NATIVE and calls:
            # ``max_parallel_tool_calls`` limits executor concurrency, not the
            # number of independent calls a provider may return in one turn.
            # Rejecting a five-call response here discarded otherwise valid
            # research work. The runtime executes the batch with bounded
            # workers and preserves the provider call order in the transcript.
            if len(calls) > 64:
                error = ModelResponseError("native response exceeds the hard tool-call safety limit: 64")
                self._log_contract_error(response, error)
                raise error
            try:
                decisions = tuple(parse_agent_decision(_tool_call_to_decision_value(call)) for call in calls)
            except ModelResponseError as exc:
                # A thinking-mode gateway can spend its output budget on
                # reasoning and truncate the tool-call arguments mid-JSON.
                # The whole turn is unusable, so repair it exactly like an
                # empty native turn instead of failing the Agent run: the
                # malformed assistant message never entered the transcript.
                return self._repair_malformed_native_turn(response, exc, tools=tools)
            if len(decisions) > 1:
                batch = AgentDecisionBatch(decisions)
                batch.validate()
                decision: AgentDecision | AgentDecisionBatch = batch
            else:
                decision = decisions[0]
            self._pending_native_message = _message_as_dict(message)
            self._pending_native_call_ids = [_tool_call_id(call) for call in calls]
            self._native_assistant_recorded = False
            return decision
        elif self._decision_mode == DecisionMode.NATIVE:
            # A few OpenAI-compatible gateways occasionally ignore
            # ``tool_choice=auto`` and return the same AgentDecision envelope
            # in message content.  Treat a valid envelope as a transport-level
            # fallback so one provider hiccup does not abort an otherwise
            # deterministic Agent loop.  Plain prose is still rejected: native
            # mode must remain structured and cannot silently invent a final
            # action from arbitrary text.
            try:
                return parse_agent_decision(_message_to_decision_value(message))
            except ModelResponseError as exc:
                return self._repair_malformed_native_turn(response, exc, tools=tools)
        try:
            return parse_agent_decision(_message_to_decision_value(message))
        except ModelResponseError as exc:
            self._log_contract_error(response, exc)
            raise

    def _repair_malformed_native_turn(
        self,
        response: Any,
        cause: ModelResponseError,
        *,
        tools: Sequence[dict[str, Any]] = (),
    ) -> AgentDecision:
        """Bounded structured repair for one unusable native provider turn."""

        repair_detail = ""
        if self.structured_fallback:
            repaired, repair_detail = self._structured_decision_fallback(response, tools=tools)
            if repaired is not None:
                return repaired
        truncated = _choice_field(response, "finish_reason") == "length"
        error = ModelResponseError(
            "native response must contain a function call or a valid AgentDecision JSON object"
            f"; provider response: {_response_digest(response)}"
            + (f"; structured repair: {repair_detail}" if repair_detail else ""),
            error_code="model_output_truncated" if truncated else "model_response_error",
        )
        self._log_contract_error(response, error)
        raise error from cause

    def _structured_decision_fallback(
        self,
        failed_response: Any,
        *,
        tools: Sequence[dict[str, Any]] = (),
    ) -> tuple[AgentDecision | None, str]:
        """Retry one malformed native turn as strict JSON.

        Some OpenAI-compatible gateways accept the tools parameter but answer a
        later turn as plain assistant content instead of a function call: the
        content is then the role deliverable (a plan or handoff object) rather
        than an AgentDecision envelope, which native mode must reject. The
        bounded JSON-schema retry explicitly requests the control envelope. A
        complete JSON business payload can be reused as failed assistant
        output and wrapped in ``final.answer``; content marked truncated is
        never reused and gets a larger, concise regeneration attempt.

        Forcing ``tool_choice`` is deliberately not used as the repair: this
        class of thinking-mode gateway rejects a named or required tool choice
        outright. The returned digest always describes the last provider
        attempt, so a caller can report why the repair did not apply.
        """
        retrying_truncation = _choice_field(failed_response, "finish_reason") == "length"
        repair_messages = list(self._session_messages)
        previous_content = _message_content(_response_message(failed_response))
        previous_object = _parse_json_object_text(previous_content)
        control_parameters = next((
            item.get("function", {}).get("parameters")
            for item in tools
            if isinstance(item, dict)
            and item.get("function", {}).get("name") == CONTROL_FUNCTION
            and isinstance(item.get("function", {}).get("parameters"), dict)
        ), None)
        control_parameters = control_parameters or decision_schema(control=True)
        reusable_business_payload = (
            not retrying_truncation
            and previous_object is not None
            and "action" not in previous_object
            and len(previous_content) <= 48_000
        )
        # Qwen-compatible gateways may return the complete role deliverable
        # as assistant content while omitting the native control call.  A
        # complete JSON object is already the business answer; sending it
        # back through the model only adds latency and can make a large
        # revision look like an endless retry.  Wrap it locally as the one
        # required final AgentDecision.  Truncated or non-object content
        # still uses the bounded provider repair below.
        if reusable_business_payload:
            return AgentDecision(
                action=AgentAction.FINAL,
                answer=previous_content,
                summary="将完整业务 JSON 本地包装为 Native final 交付",
            ), "local_business_json_wrap"
        repair_messages.append({
            "role": "user",
            "content": (
                "上一次响应没有遵守 Agent 协议。现在只输出一个合法 AgentDecision，"
                "Native 模式下通过 agent_control 完成。遵循当前工具 Schema 中 answer 的类型："
                "若 answer 是对象，直接提交业务 JSON 对象；若 answer 是字符串，再放入序列化 JSON。"
                "不能将业务 JSON 写入普通 assistant content。"
                + (
                    "上一次输出因长度上限被截断，请依据原始任务重新生成完整且更精简的业务 JSON，"
                    "保留必需字段、有效证据和结论，避免重复内容。"
                    if retrying_truncation else
                    "请依据原始任务完成交付，不要输出普通文字或业务 JSON 作为外层响应。"
                )
            ),
        })
        retry_token_budget = self.max_retry_tokens if retrying_truncation else self.max_tokens
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": repair_messages,
            "temperature": 0,
            "max_tokens": retry_token_budget,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_decision",
                    "strict": True,
                    # Request exactly the contract the native ``agent_control``
                    # function exposes. A wider schema lets a gateway fill
                    # action fields the strict internal contract then rejects,
                    # which turns one repair into a second failure: a live
                    # gateway answered with "final" plus a junk
                    # ``arguments``/``capability`` payload, and every such turn
                    # failed validation instead of repairing the run.
                    "schema": control_parameters,
                },
            },
        }
        if self.extra_body:
            payload["extra_body"] = self.extra_body
        repair_profile = ProviderContextProfile.from_env(self.model_name, retry_token_budget)
        repair_report = build_context_budget_report(
            role=self.budget_role,
            messages=repair_messages,
            tools=(),
            profile=repair_profile,
        )
        if repair_report.over_hard_limit:
            self.last_budget_report = repair_report
            self._log_local_error(
                repair_messages,
                error_code="context_budget_exceeded",
                message="structured repair prompt exceeds the provider hard input budget",
            )
            raise ModelRequestError(
                "structured repair prompt exceeds the provider hard input budget",
                error_code="context_budget_exceeded",
            )
        attempts: list[str] = []
        for attempt in range(2):
            if attempt:
                # A gateway may reject response_format; retry once without it.
                payload.pop("response_format", None)
            try:
                response = self._request(payload)
            except ModelRequestError:
                raise
            except ModelResponseError as exc:
                attempts.append(f"attempt {attempt + 1} request failed: {exc}")
                continue
            digest = _response_digest(response)
            attempts.append(f"attempt {attempt + 1}: {digest}")
            try:
                return parse_agent_decision(_message_to_decision_value(_response_message(response))), digest
            except ModelResponseError as exc:
                attempts[-1] = f"{attempts[-1]}, rejected: {exc}"
                continue
        return None, "; ".join(attempts) or "no provider response"

    def record_result(self, result: dict[str, Any]) -> None:
        """Append the standard native tool result for the pending call."""
        if self._decision_mode != DecisionMode.NATIVE or self._pending_native_message is None or not self._pending_native_call_ids:
            return
        if not self._native_assistant_recorded:
            self._session_messages.append(self._pending_native_message)
            self._native_assistant_recorded = True
        call_id = self._pending_native_call_ids.pop(0)
        self._session_messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False, sort_keys=True, default=str),
        })
        if not self._pending_native_call_ids:
            self._pending_native_message = None
            self._native_assistant_recorded = False

    def _request(self, payload: dict[str, Any], *, request_id: str | None = None) -> Any:
        """Retry transient failures and one sanitized provider-policy recovery.

        A provider content-inspection refusal is not made retryable by sending
        the same request again.  One bounded recovery removes raw tool/source
        text while retaining identifiers and URLs, allowing the Agent to
        search or fetch a safer representation.  The durable source store is
        unaffected; this only changes the provider-facing retry payload.
        """
        request_id = request_id or f"req_{uuid4().hex}"
        # The projection may have been selected by a prior request in this
        # same Agent session.  Start recovery at that level so a repeated
        # provider refusal cannot loop through the same sanitizer again.
        content_recovery_attempted = self._provider_source_projection in {"sanitized", "stripped"}
        source_context_recovery_attempted = self._provider_source_projection == "stripped"
        transient_failures = 0
        attempt = 0
        while True:
            attempt += 1
            self.last_budget_report = self._budget_report_for_payload(payload)
            request_timeout = self._request_timeout()
            try:
                send = lambda: self.transport(self.base_url, self.api_key, payload)
                response = (
                    self.transport_guard(self.base_url, self.api_key, payload, send, request_timeout)
                    if self.transport_guard is not None else send()
                )
            except ModelRequestError as exc:
                if (
                    exc.error_code == "provider_policy_rejected"
                    and _payload_has_raw_source_text(payload)
                ):
                    if not content_recovery_attempted:
                        self._log_provider_error(payload, request_id, attempt, exc, recovery_action="sanitize_source_text")
                        self._provider_source_projection = "sanitized"
                        payload = _sanitize_provider_payload(payload)
                        content_recovery_attempted = True
                        continue
                    if not source_context_recovery_attempted:
                        self._log_provider_error(payload, request_id, attempt, exc, recovery_action="strip_source_context")
                        self._provider_source_projection = "stripped"
                        payload = _strip_provider_source_context(payload)
                        source_context_recovery_attempted = True
                        continue
                self._log_provider_error(payload, request_id, attempt, exc)
                raise
            except Exception as exc:
                self._request_timeout()
                if getattr(exc, "retryable", True) is False and getattr(exc, "error_code", None):
                    failure = ModelRequestError(str(exc), error_code=str(exc.error_code))
                    self._log_provider_error(payload, request_id, attempt, failure, cause=exc)
                    raise failure from exc
                code = _provider_error_code(exc)
                if code in {"data_inspection_failed", "content_filter", "content_policy_violation"}:
                    if _payload_has_raw_source_text(payload):
                        if not content_recovery_attempted:
                            self._log_provider_error(payload, request_id, attempt, ModelRequestError("model provider rejected the content", error_code="provider_policy_rejected"), cause=exc, recovery_action="sanitize_source_text")
                            self._provider_source_projection = "sanitized"
                            payload = _sanitize_provider_payload(payload)
                            content_recovery_attempted = True
                            continue
                        if not source_context_recovery_attempted:
                            self._log_provider_error(payload, request_id, attempt, ModelRequestError("model provider rejected the content", error_code="provider_policy_rejected"), cause=exc, recovery_action="strip_source_context")
                            self._provider_source_projection = "stripped"
                            payload = _strip_provider_source_context(payload)
                            source_context_recovery_attempted = True
                            continue
                    failure = ModelRequestError("model provider rejected the content", error_code="provider_policy_rejected")
                    self._log_provider_error(payload, request_id, attempt, failure, cause=exc)
                    raise failure from exc
                status = getattr(exc, "status_code", None)
                classified = _classify_provider_error(exc, status=status, provider_code=code)
                if classified == "provider_context_length":
                    failure = ModelRequestError("model provider rejected the request because the context is too large", error_code=classified)
                    self._log_provider_error(payload, request_id, attempt, failure, cause=exc)
                    raise failure from exc
                transient = (
                    status in {408, 409, 429} or (isinstance(status, int) and status >= 500)
                    or (status is None and isinstance(exc, (OSError, TimeoutError)))
                    or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}
                )
                if not transient or transient_failures >= self.max_retries:
                    failure = ModelRequestError(
                        f"model request failed after {transient_failures + 1} attempts: {exc}",
                        error_code=classified,
                    )
                    self._log_provider_error(payload, request_id, attempt, failure, cause=exc)
                    raise failure from exc
                self._log_provider_error(payload, request_id, attempt, ModelRequestError(str(exc), error_code=classified), cause=exc, recovery_action="retry")
                # Backoff belongs to this one loop, never the SDK or Controller.
                delay = min(0.5 * (2 ** min(transient_failures, 4)), self._request_timeout())
                transient_failures += 1
                time.sleep(delay)
                continue
            self._request_timeout()  # Never accept a response after the task deadline.
            if _provider_response_rejected(response):
                if _payload_has_raw_source_text(payload):
                    if not content_recovery_attempted:
                        self._log_provider_error(payload, request_id, attempt, ModelRequestError("model provider refused the response", error_code="provider_policy_rejected"), response=response, recovery_action="sanitize_source_text")
                        self._provider_source_projection = "sanitized"
                        payload = _sanitize_provider_payload(payload)
                        content_recovery_attempted = True
                        continue
                    if not source_context_recovery_attempted:
                        self._log_provider_error(payload, request_id, attempt, ModelRequestError("model provider refused the response", error_code="provider_policy_rejected"), response=response, recovery_action="strip_source_context")
                        self._provider_source_projection = "stripped"
                        payload = _strip_provider_source_context(payload)
                        source_context_recovery_attempted = True
                        continue
                failure = ModelRequestError("model provider refused the response", error_code="provider_policy_rejected")
                self._log_provider_error(payload, request_id, attempt, failure, response=response)
                raise failure
            self.last_provider_diagnostics = _provider_response_metadata(response, request_id=request_id, attempt=attempt)
            self._log_agent_turn(payload, response=response, request_id=request_id, attempt=attempt)
            return response

    def _budget_report_for_payload(self, payload: dict[str, Any]) -> ContextBudgetReport:
        max_tokens = payload.get("max_tokens", self.max_tokens)
        try:
            output_reserve = max(1, int(max_tokens))
        except (TypeError, ValueError):
            output_reserve = self.max_tokens
        profile = ProviderContextProfile.from_env(self.model_name, output_reserve)
        return build_context_budget_report(
            role=self.budget_role,
            messages=payload.get("messages") or (),
            tools=payload.get("tools") or (),
            profile=profile,
        )

    def _log_provider_error(
        self,
        payload: dict[str, Any],
        request_id: str,
        attempt: int,
        error: ModelRequestError,
        *,
        cause: Exception | None = None,
        response: Any | None = None,
        recovery_action: str | None = None,
    ) -> None:
        self.last_provider_diagnostics = _provider_response_metadata(response, request_id=request_id, attempt=attempt, error=cause or error)
        event = self._diagnostic_event(payload, request_id=request_id, attempt=attempt)
        event.update({
            "kind": "provider_error",
            "error_code": error.error_code,
            "http_status": self.last_provider_diagnostics.get("http_status"),
            "provider_code": _provider_error_code(cause or error) or None,
            "provider_request_id": self.last_provider_diagnostics.get("provider_request_id"),
            "recovery_action": recovery_action,
            "message_summary": _provider_error_summary(cause or error),
            "response_summary": _response_digest(response) if response is not None else None,
        })
        self._write_diagnostic("provider_error", event)

    def _log_agent_turn(self, payload: dict[str, Any], *, response: Any, request_id: str, attempt: int) -> None:
        event = self._diagnostic_event(payload, request_id=request_id, attempt=attempt)
        metadata = _provider_response_metadata(response, request_id=request_id, attempt=attempt)
        event.update({
            "kind": "agent_turn",
            "provider_request_id": metadata.get("provider_request_id"),
            "finish_reason": metadata.get("finish_reason"),
            "usage": _extract_usage(response),
            "response_summary": _response_digest(response),
            "response_hash": _response_hash(response),
        })
        if self.last_compaction is not None:
            event["compaction"] = self.last_compaction
            self._write_diagnostic("context_compaction", {
                **self._diagnostic_event(payload, request_id=request_id, attempt=attempt),
                "kind": "context_compaction",
                **self.last_compaction,
            })
        self._write_diagnostic("agent_turn", event)

    def _diagnostic_event(self, payload: dict[str, Any], *, request_id: str, attempt: int) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        messages = payload.get("messages") or []
        prompt_hash = sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        event: dict[str, Any] = {
            **self.diagnostic_context,
            **_context_fields_from_messages(messages),
            "request_id": request_id,
            "attempt": attempt,
            "model": self.model_name,
            "decision_mode": self._decision_mode.value,
            "payload_hash": f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}",
            "prompt_hash": f"sha256:{prompt_hash}",
            "message_count": len(messages),
            "tool_schema_count": len(payload.get("tools") or []),
        }
        if self.last_budget_report is not None:
            report = self.last_budget_report.to_dict()
            event.update({key: report[key] for key in (
                "input_chars", "input_tokens_estimated", "request_overhead_tokens_estimated",
                "target_tokens", "soft_limit_tokens", "hard_limit_tokens",
                "context_pack_hash", "context_pack_chars", "context_pack_layer_stats",
                "context_field_chars", "context_field_tokens", "system_chars", "system_tokens_estimated",
                "user_chars", "user_tokens_estimated", "transcript_chars", "transcript_tokens_estimated",
                "tool_schema_chars", "tool_schema_tokens_estimated",
            ) if key in report})
        if self.last_compaction is not None:
            event["compaction"] = self.last_compaction
        return event

    def _log_local_error(
        self,
        messages: list[dict[str, Any]],
        *,
        error_code: str,
        message: str,
        tools: Sequence[dict[str, Any]] = (),
    ) -> None:
        request_id = f"req_{uuid4().hex}"
        payload = {"messages": messages, "tools": list(tools)}
        event = self._diagnostic_event(payload, request_id=request_id, attempt=1)
        event.update({
            "kind": "provider_error",
            "error_code": error_code,
            "recovery_action": "stop_before_transport",
            "message_summary": message,
        })
        self._write_diagnostic("provider_error", event)
        if self.last_compaction is not None:
            self._write_diagnostic("context_compaction", {
                **self._diagnostic_event(payload, request_id=request_id, attempt=1),
                "kind": "context_compaction",
                **self.last_compaction,
            })

    def _log_contract_error(self, response: Any, error: ModelResponseError) -> None:
        request_id = str(self.last_provider_diagnostics.get("request_id") or f"req_{uuid4().hex}")
        event = self._diagnostic_event(
            {"messages": self._session_messages},
            request_id=request_id,
            attempt=int(self.last_provider_diagnostics.get("attempt") or 1),
        )
        event.update({
            "kind": "agent_turn",
            "error_code": error.error_code,
            "finish_reason": _choice_field(response, "finish_reason"),
            "response_summary": _response_digest(response),
            "response_hash": _response_hash(response),
        })
        self._write_diagnostic("agent_turn", event)

    def _write_diagnostic(self, stream: str, event: dict[str, Any]) -> None:
        if self.diagnostics is None:
            return
        try:
            self.diagnostics.write(stream, event)
        except Exception:
            return

    def _request_timeout(self) -> float:
        if self.deadline is None:
            return self.timeout_seconds
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ModelDeadlineExceeded()
        return min(self.timeout_seconds, remaining)

    def _default_transport(self, base_url: str, api_key: str, payload: dict[str, Any]) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelResponseError("openai package is required for live model calls") from exc
        # Retries are owned by ``_request`` so injected transports and the
        # OpenAI SDK have the same bounded behavior. The SDK itself must not
        # add another hidden retry loop.
        with OpenAI(base_url=base_url, api_key=api_key, timeout=self._request_timeout(), max_retries=0) as client:
            return client.chat.completions.create(**payload)


def _provider_error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("code"):
            return str(nested["code"])
    text = str(error).lower()
    for candidate in (
        "data_inspection_failed", "content_policy_violation", "content_filter",
        "context_length_exceeded", "context_window_exceeded", "max_tokens_exceeded", "request_too_large",
    ):
        if candidate in text:
            return candidate
    return ""


def _provider_status(error: Exception | None) -> int | None:
    if error is None:
        return None
    for name in ("status_code", "http_status", "status"):
        value = getattr(error, name, None)
        if isinstance(value, int):
            return value
    return None


def _classify_provider_error(error: Exception, *, status: Any = None, provider_code: str = "") -> str:
    code = (provider_code or _provider_error_code(error) or "").lower()
    status_code = status if isinstance(status, int) else _provider_status(error)
    if code in {"context_length_exceeded", "context_window_exceeded", "max_tokens_exceeded", "request_too_large"} or status_code == 413:
        return "provider_context_length"
    if code in {"data_inspection_failed", "content_filter", "content_policy_violation", "safety_violation"}:
        return "provider_policy_rejected"
    if status_code in {408, 409, 429} or (isinstance(status_code, int) and status_code >= 500):
        return "provider_unavailable"
    if isinstance(error, TimeoutError) or type(error).__name__ in {"APITimeoutError", "APIConnectionError"}:
        return "provider_unavailable"
    return "provider_request_failed"


def _provider_response_rejected(response: Any) -> bool:
    """Return whether a successful HTTP response contains a policy refusal."""

    try:
        message = _response_message(response)
    except ModelResponseError:
        return False
    refusal = message.get("refusal") if isinstance(message, dict) else getattr(message, "refusal", None)
    return bool(refusal or _choice_field(response, "finish_reason") == "content_filter")


_RAW_PROVIDER_FIELDS = {
    "content", "raw_content", "excerpt", "snippet", "quote", "quoted_text",
    "passage", "source_text", "raw_text", "source_excerpt", "html", "body",
}
_SOURCE_CONTEXT_FIELDS = {
    "summary", "text", "title", "author", "description", "why_relevant",
    "conclusion", "caption",
}
_SOURCE_CONTEXT_PARENTS = {
    "source_documents", "documents", "evidence", "output", "document", "record",
    "document_card", "relevant_excerpts", "claims", "media", "state", "current_draft",
}


def _sanitize_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove raw source text from a single provider recovery request.

    Source records remain durable in MySQL.  The recovery request keeps
    identifiers, titles, URLs, hashes and search arguments so the Agent can
    reacquire a needed passage through a fresh Tool call.
    """

    sanitized = dict(payload)
    messages = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            messages.append(message)
            continue
        item = dict(message)
        if item.get("role") in {"user", "tool"} and isinstance(item.get("content"), str):
            raw = item["content"]
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                item["content"] = "[source text omitted after provider content inspection; use source IDs and URLs]"
            else:
                item["content"] = json.dumps(_redact_source_text(decoded), ensure_ascii=False, sort_keys=True, default=str)
        messages.append(item)
    sanitized["messages"] = messages
    return sanitized


def _strip_provider_source_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the final bounded recovery request after a second refusal.

    Keep the task, source identifiers and URLs so the Agent can issue a fresh
    search/fetch call, but remove source-bearing transcript entries and draft
    text that may have escaped the field-level sanitizer. This is deliberately
    limited to one additional attempt; repeated policy retries cannot improve
    a request that the provider still refuses.
    """

    sanitized = dict(payload)
    messages: list[Any] = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            messages.append(message)
            continue
        item = dict(message)
        role = item.get("role")
        if role == "tool":
            item["content"] = "{\"status\":\"source text omitted after repeated provider inspection refusal\"}"
        elif role == "user" and isinstance(item.get("content"), str):
            try:
                decoded = json.loads(item["content"])
            except (TypeError, ValueError, json.JSONDecodeError):
                item["content"] = "{\"source_context\":\"omitted after repeated provider inspection refusal\"}"
            else:
                item["content"] = json.dumps(
                    _strip_source_context_value(decoded),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
        messages.append(item)
    sanitized["messages"] = messages
    return sanitized


def _strip_source_context_value(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in {"current_draft", "review_feedback", "dependency_node_outputs"}:
                result[key_text] = {"status": "omitted after repeated provider inspection refusal"}
                continue
            if key_text == "source_documents":
                result[key_text] = _source_identity_only(item)
                continue
            if key_text == "recent_observations":
                result[key_text] = [{"type": "omitted_after_provider_inspection"}]
                continue
            if _is_raw_provider_source_field(key_text, parent_key):
                result[key_text] = "[omitted after repeated provider inspection refusal]"
            elif key_text in {"source_documents", "documents", "evidence", "media", "current_draft", "recent_observations"}:
                result[key_text] = _strip_source_context_value(item, parent_key=key_text)
            else:
                result[key_text] = _strip_source_context_value(item, parent_key=key_text)
        return result
    if isinstance(value, list):
        return [_strip_source_context_value(item, parent_key=parent_key) for item in value[:20]]
    return value


def _source_identity_only(value: Any) -> Any:
    if isinstance(value, list):
        return [_source_identity_only(item) for item in value[:20]]
    if not isinstance(value, dict):
        return None
    return {
        key: value[key]
        for key in ("document_id", "source", "url", "canonical_url", "content_hash", "published_at", "fetched_at")
        if key in value
    }


def _payload_has_raw_source_text(payload: dict[str, Any]) -> bool:
    """Detect source-bearing records before attempting policy recovery."""

    for message in payload.get("messages", []):
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            continue
        try:
            decoded = json.loads(message["content"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _contains_raw_source_field(decoded):
            return True
    return False


def _contains_raw_source_field(value: Any, *, parent_key: str | None = None) -> bool:
    if isinstance(value, dict):
        if any(_is_raw_provider_source_field(str(key), parent_key) for key in value):
            return True
        return any(
            _contains_raw_source_field(item, parent_key=str(key))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_raw_source_field(item, parent_key=parent_key) for item in value)
    return False


def _is_raw_provider_source_field(key: str, parent_key: str | None) -> bool:
    return key in _RAW_PROVIDER_FIELDS or (
        key in _SOURCE_CONTEXT_FIELDS and parent_key in _SOURCE_CONTEXT_PARENTS
    )


def _redact_source_text(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[omitted after provider content inspection]"
                if _is_raw_provider_source_field(str(key), parent_key)
                else _redact_source_text(item, parent_key=str(key))
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_source_text(item, parent_key=parent_key) for item in value]
    return value


def _choice_field(response: Any, name: str) -> Any:
    """Read one field from the first provider choice for diagnostics."""

    if isinstance(response, dict):
        choices = response.get("choices") or []
        first = choices[0] if choices else None
        if isinstance(first, dict):
            return first.get(name)
        return getattr(first, name, None) if first is not None else None
    choices = getattr(response, "choices", None) or []
    first = choices[0] if choices else None
    return getattr(first, name, None) if first is not None else None


def _response_digest(response: Any) -> str:
    """Summarize a provider response so a malformed turn stays diagnosable.

    The Agent loop reports failures through a short error string, and a bare
    "no usable decision" message hides whether the gateway truncated the turn,
    returned reasoning only, or answered in prose. The digest records bounded
    metadata (finish reason, output-budget stop, content length/hash, reasoning
    length, tool-call count, usage) without persisting response text.
    """

    parts: list[str] = []
    finish_reason = _choice_field(response, "finish_reason")
    if finish_reason is not None:
        parts.append(f"finish_reason={finish_reason}")
        if finish_reason == "length":
            parts.append("provider stopped at the configured max_tokens output budget")
    try:
        message = _response_message(response)
    except ModelResponseError:
        parts.append("message=unavailable")
        return ", ".join(parts)
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if content is None:
        parts.append("content=missing")
    else:
        text = str(content)
        parts.append(f"content_chars={len(text)}")
        parts.append(f"content_hash=sha256:{sha256(text.encode('utf-8')).hexdigest()}")
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else getattr(message, "reasoning_content", None)
    if reasoning:
        parts.append(f"reasoning_content={len(str(reasoning))} chars")
    parts.append(f"tool_calls={len(_message_tool_calls(message))}")
    usage = _extract_usage(response)
    if usage:
        parts.append("usage=" + json.dumps(usage, sort_keys=True))
    return ", ".join(parts)


def _provider_response_metadata(
    response: Any | None,
    *,
    request_id: str,
    attempt: int,
    error: Exception | None = None,
) -> dict[str, Any]:
    headers = None
    if response is not None:
        headers = response.get("headers") if isinstance(response, dict) else getattr(response, "headers", None)
    provider_request_id = None
    for source in (response, headers, error, getattr(error, "response", None) if error else None):
        if source is None:
            continue
        for name in ("_request_id", "request_id", "x-request-id", "request-id", "id"):
            value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
            if value:
                provider_request_id = str(value)[:200]
                break
        if provider_request_id:
            break
    return {
        "request_id": request_id,
        "attempt": attempt,
        "http_status": _provider_status(error) or _response_status(response),
        "provider_code": (_provider_error_code(error) or None) if error is not None else None,
        "provider_request_id": provider_request_id,
        "finish_reason": _choice_field(response, "finish_reason") if response is not None else None,
        "usage": _extract_usage(response) if response is not None else {},
    }


def _response_status(response: Any | None) -> int | None:
    if response is None:
        return None
    for name in ("status_code", "http_status", "status"):
        value = response.get(name) if isinstance(response, dict) else getattr(response, name, None)
        if isinstance(value, int):
            return value
    return None


def _response_hash(response: Any) -> str:
    try:
        if isinstance(response, dict):
            value = response
        else:
            value = {
                "id": getattr(response, "id", None),
                "model": getattr(response, "model", None),
                "choices": [{
                    "finish_reason": _choice_field(response, "finish_reason"),
                    "message": _message_as_dict(_response_message(response)),
                }],
                "usage": _extract_usage(response),
            }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        encoded = repr(type(response))
    return f"sha256:{sha256(encoded.encode('utf-8')).hexdigest()}"


def _compact_context_pack_messages(
    messages: list[dict[str, Any]], level: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        try:
            payload = json.loads(message["content"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("context_pack"), dict):
            continue
        old_pack = payload["context_pack"]
        new_pack, metadata = compact_context_pack(old_pack, level)
        if new_pack.get("pack_hash") == old_pack.get("pack_hash"):
            return None
        payload["context_pack"] = new_pack
        candidate = [dict(item) for item in messages]
        candidate[index]["content"] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return candidate, metadata
    return None


def _compaction_id(before_hash: str | None, after_hash: str | None) -> str:
    seed = f"{before_hash or ''}:{after_hash or ''}".encode("utf-8")
    return "compact_" + sha256(seed).hexdigest()[:20]


def _context_fields_from_messages(messages: Any) -> dict[str, Any]:
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        try:
            decoded = json.loads(content) if isinstance(content, str) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict):
            continue
        pack = decoded.get("context_pack") if isinstance(decoded.get("context_pack"), dict) else {}
        task = pack.get("task") if isinstance(pack.get("task"), dict) else {}
        state = pack.get("state") if isinstance(pack.get("state"), dict) else {}
        draft = state.get("current_draft") if isinstance(state.get("current_draft"), dict) else {}
        fields = {
            "run_id": pack.get("run_id"),
            "node_id": task.get("node_id") or draft.get("node_id"),
            "slot_id": task.get("slot_id") or draft.get("slot_id"),
            "revision": task.get("revision") or draft.get("revision"),
            "step": decoded.get("agent_step"),
        }
        return {key: value for key, value in fields.items() if value is not None}
    return {}


def _safe_diagnostic_text(value: str, *, max_chars: int = 500) -> str:
    text = " ".join(str(value).split())
    text = text.replace("Bearer ", "Bearer [redacted]")
    text = text.split(" response body:", 1)[0]
    text = text.split(" body=", 1)[0]
    return text[:max_chars]


def _provider_error_summary(error: Exception) -> str:
    status = _provider_status(error)
    code = _provider_error_code(error)
    parts = [type(error).__name__]
    if status is not None:
        parts.append(f"http_status={status}")
    if code:
        parts.append(f"provider_code={code[:120]}")
    if isinstance(error, TypeError):
        detail = _safe_diagnostic_text(str(error), max_chars=180)
        if detail:
            parts.append(f"detail={detail}")
    return "; ".join(parts)


def _message_to_decision_value(message: Any) -> Any:
    """Convert native function/tool calls to the stable AgentDecision envelope."""

    if isinstance(message, dict):
        tool_calls = message.get("tool_calls") or []
        content = message.get("content")
    else:
        tool_calls = getattr(message, "tool_calls", None) or []
        content = getattr(message, "content", None)
    if tool_calls:
        return _tool_call_to_decision_value(tool_calls[0])
    if content is None:
        raise ModelResponseError("model response content is missing")
    return content


def _message_content(message: Any) -> str:
    """Return assistant text without coercing missing content to a fake value."""

    if isinstance(message, dict):
        value = message.get("content")
    else:
        value = getattr(message, "content", None)
    return value if isinstance(value, str) else ""


def _parse_json_object_text(value: str) -> dict[str, Any] | None:
    """Parse a complete JSON object used as a Skill business payload."""

    if not value or not value.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_call_to_decision_value(call: Any) -> dict[str, Any]:
    function = call.get("function", {}) if isinstance(call, dict) else getattr(call, "function", None)
    name = function.get("name") if isinstance(function, dict) else getattr(function, "name", None)
    raw_arguments = function.get("arguments", "{}") if isinstance(function, dict) else getattr(function, "arguments", "{}")
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments or {})
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ModelResponseError("native tool call arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ModelResponseError("native tool call arguments must be an object")
    if name == CONTROL_FUNCTION:
        action = arguments.get("action")
        answer = arguments.get("answer")
        if isinstance(answer, dict):
            answer = json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
        return {
            "action": action,
            "summary": str(arguments.get("summary") or f"Control action: {action or 'unknown'}.").strip(),
            # Native providers sometimes echo unrelated nullable fields from
            # the strict control schema. Normalize them at the provider
            # boundary so the internal AgentDecision contract remains exact.
            "skill": arguments.get("skill") if action == "activate_skill" else None,
            "reference": arguments.get("reference") if action == "read_skill_reference" else None,
            "answer": answer if action in {"final", "research_checkpoint", "request_human"} else None,
        }
    return {"action": "call_tool", "summary": f"Call {name}.", "capability": name, "arguments": arguments}


def _response_message(response: Any) -> Any:
    if isinstance(response, dict):
        try:
            return response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError("model response choices are malformed") from exc
    try:
        return response.choices[0].message
    except (AttributeError, IndexError, TypeError) as exc:
        raise ModelResponseError("model response content is missing") from exc


def _message_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, dict):
        return list(message.get("tool_calls") or [])
    return list(getattr(message, "tool_calls", None) or [])


def _tool_call_id(call: Any) -> str:
    value = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
    if not value:
        raise ModelResponseError("native tool call id is missing")
    return str(value)


def _message_as_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        result = dict(message)
        result.setdefault("role", "assistant")
        return result
    result: dict[str, Any] = {"role": getattr(message, "role", "assistant")}
    content = getattr(message, "content", None)
    if content is not None:
        result["content"] = content
    calls = []
    for call in _message_tool_calls(message):
        function = getattr(call, "function", None)
        calls.append({
            "id": getattr(call, "id", None),
            "type": getattr(call, "type", "function"),
            "function": {
                "name": getattr(function, "name", None),
                "arguments": getattr(function, "arguments", "{}"),
            },
        })
    if calls:
        result["tool_calls"] = calls
    return result


def _extract_usage(response: Any) -> dict[str, int]:
    """Capture provider usage without making it part of the Agent contract."""
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {str(key): int(value) for key, value in usage.items() if isinstance(value, (int, float))}
    return {
        key: int(value)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if (value := getattr(usage, key, None)) is not None
    }


def parse_agent_decision(value: AgentDecision | dict[str, Any] | str) -> AgentDecision:
    """Parse and validate one provider response at the model boundary."""

    if isinstance(value, AgentDecision):
        value.validate()
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ModelResponseError("model response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelResponseError("model response must be an AgentDecision object")

    # Some Responses/Chat adapters wrap structured output in a decision key.
    if isinstance(value.get("decision"), dict):
        value = value["decision"]
    try:
        action = AgentAction(str(value.get("action", "")))
    except ValueError as exc:
        raise ModelResponseError(f"unknown Agent action: {value.get('action')!r}") from exc

    raw_arguments = value.get("arguments")
    if raw_arguments is None:
        raw_arguments = {}
    if not isinstance(raw_arguments, dict):
        raise ModelResponseError("model decision arguments must be an object")

    action_summary = str(value.get("summary") or "").strip()
    if not action_summary:
        action_summary = f"{action.value.replace('_', ' ').capitalize()}."
    raw_answer = value.get("answer")
    if isinstance(raw_answer, dict):
        raw_answer = json.dumps(raw_answer, ensure_ascii=False, separators=(",", ":"))
    decision = AgentDecision(
        action=action,
        summary=action_summary,
        skill=_optional_string(value.get("skill")),
        reference=_optional_string(value.get("reference")),
        capability=_optional_string(value.get("capability")),
        arguments=dict(raw_arguments),
        answer=_optional_string(raw_answer),
    )
    try:
        decision.validate()
    except (TypeError, ValueError) as exc:
        raise ModelResponseError(str(exc)) from exc
    return decision


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    # Strict JSON schemas mark these fields nullable, and some gateways then
    # echo the JSON literal back as the string "null" instead of a real null.
    # Treat that as absent; otherwise an otherwise valid decision is rejected
    # for carrying a field its action does not accept.
    if not text or text.lower() == "null":
        return None
    return text
