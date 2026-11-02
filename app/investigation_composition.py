"""Composition root for the MySQL-backed node investigation runtime."""

from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Any

from sqlalchemy import select

from echotrace.agent import (
    AgentRuntime,
    AgentSkillCatalog,
    InvestigationRunTraceSink,
    NODE_ROLE_PROMPTS,
    OpenAICompatibleModelClient,
    build_context_pack,
)
from echotrace.agent.context_budget import estimate_tokens
from echotrace.agent.research_progress import (
    ResearchProgress,
    SEARCH_CAPABILITIES,
    contract_error_requires_research,
)
from echotrace.application import InvestigationTaskService
from echotrace.application.investigation_service import TaskDeferredError
from echotrace.application.report_composer import (
    assemble_report,
    build_report_material_pack,
    build_source_limits_fallback_section,
    generate_report_section,
    plan_report,
    section_citation_warnings,
)
from echotrace.application.report_templates import load_report_template
from echotrace.application.node_trace import NodeTrace, refresh_frozen_trace
from echotrace.domain.models import Document, InvestigationRun, InvestigationStatus, beijing_now, new_id
from echotrace.domain.agent_roles import AgentRole
from echotrace.domain.trace_nodes import ConclusionIR, NodeReview, TraceContractError, get_trace_template
from echotrace.domain.reporting import REPORT_BLOCK_TYPES, ReportBlock, ReportDocument
from echotrace.infrastructure.persistence.mysql import (
    Database,
    SqlAlchemyRunStore,
    SqlAlchemyTraceNodeStore,
    SqlAlchemyUnitOfWork,
    create_database,
)
from echotrace.infrastructure.persistence.mysql.models import SourceDocument
from echotrace.infrastructure.object_store import S3CompatibleObjectStore
from echotrace.infrastructure.object_store_config import ObjectStoreConfig
from echotrace.infrastructure.coordination.redis import (
    OrchestratorRunWorker,
    OutboxDispatcher,
    ReportComposerWorker,
    RedisConfig,
    RedisCallCoordinator,
    RedisCallGateError,
    RedisLeaseManager,
    RedisPermitManager,
    RedisRateLimiter,
    RedisResponseCache,
    RedisStreamBus,
    StreamTaskWorker,
)
from echotrace.runtime.node_controller import (
    NodeController,
    NodeControllerPolicy,
    validate_conclusion_ir_content,
)
from echotrace.runtime.node_planner import NodeTaskPlanner
from echotrace.runtime.node_workflow import NodeInvestigationController
from echotrace.runtime.node_worker import NodeRoleTaskHandler
from echotrace.runtime.policies import InvestigationPolicy
from echotrace.runtime.tool_gateway import ToolRouter, ToolUnavailable


@dataclass(slots=True)
class InvestigationRuntime:
    run_store: SqlAlchemyRunStore
    controller: NodeInvestigationController
    task_service: InvestigationTaskService
    redis_bus: RedisStreamBus
    dispatcher: OutboxDispatcher
    orchestrator_worker: OrchestratorRunWorker
    report_worker: ReportComposerWorker | None = None
    node_workers: tuple[StreamTaskWorker, ...] = ()
    database: Database | None = None
    object_store: S3CompatibleObjectStore | None = None

    def enqueue_run(self, run_id: str, *, idempotency_key: str | None = None) -> str:
        return self.task_service.enqueue_run_start(run_id=run_id, idempotency_key=idempotency_key)

    def dispatch_once(self, *, owner: str = "api-dispatcher", limit: int = 50):
        return self.dispatcher.dispatch_once(owner=owner, limit=limit)

    def close(self) -> None:
        client = getattr(self.redis_bus, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()
        if self.database is not None:
            self.database.close()

    def reformat_frozen_node(self, run_id: str, slot_id: str) -> dict[str, Any]:
        run = self.run_store.get(run_id)
        if run is None:
            raise KeyError(f"investigation not found: {run_id}")
        if run.report_document or str(run.status.value) == "completed":
            raise ValueError("frozen node formatting cannot be refreshed after report generation")
        template = get_trace_template(str(run.investigation_type or "public_event"))
        node = next((item for item in self.controller.node_store.list_nodes(run_id) if item.get("slot_id") == slot_id), None)
        if node is None:
            raise KeyError(f"investigation node not found: {slot_id}")
        revision_no = node.get("frozen_revision")
        if node.get("status") != "frozen" or not isinstance(revision_no, int):
            raise ValueError("only frozen nodes can have their display formatting refreshed")
        ir = self.controller.controller.reformat_frozen_revision(
            run_id=run_id,
            objective=run.objective,
            template=template,
            slot_id=slot_id,
            revision_no=revision_no,
        )
        return {"run_id": run_id, "slot_id": slot_id, "revision": revision_no, "conclusion_ir": ir.to_dict()}


def _llm_thinking_config() -> tuple[bool, int, dict[str, Any] | None]:
    thinking = os.getenv("ECHOTRACE_LLM_THINKING", "0").strip() == "1"
    try:
        thinking_max_tokens = max(1, int(os.getenv("ECHOTRACE_LLM_THINKING_MAX_TOKENS", "65536")))
    except ValueError:
        thinking_max_tokens = 65536
    extra_body = dict(_llm_extra_body() or {})
    extra_body["enable_thinking"] = thinking
    return thinking, thinking_max_tokens, extra_body


def _make_model_transport_guard(redis_coordinator: Any, role: str):
    """Apply shared Redis limits using a serialized, multilingual request estimate."""

    def transport_guard(
        base_url: str,
        credential: str,
        payload: dict[str, Any],
        send: Any,
        timeout_seconds: float,
    ):
        if redis_coordinator is None:
            return send()
        provider_id = "llm:" + sha256(base_url.encode("utf-8")).hexdigest()[:16]
        try:
            reserved_output_tokens = max(1, int(os.getenv("ECHOTRACE_LLM_RESERVED_OUTPUT_TOKENS", "6000")))
            request_content = json.dumps(
                {"messages": payload.get("messages") or [], "tools": payload.get("tools") or []},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            token_cost = estimate_tokens(request_content) + min(
                int(payload.get("max_tokens") or reserved_output_tokens), reserved_output_tokens,
            )
            request_limit = max(1, int(os.getenv("ECHOTRACE_LLM_REQUESTS_PER_MINUTE", "60")))
            default_token_limit = os.getenv("ECHOTRACE_LLM_CONTEXT_WINDOW", "1000000")
            token_limit = max(1, int(os.getenv("ECHOTRACE_LLM_TOKENS_PER_MINUTE", default_token_limit)))
            provider_limit = max(1, int(os.getenv("ECHOTRACE_LLM_PROVIDER_MAX_CONCURRENT", "8")))
            role_limit = max(1, int(os.getenv(f"ECHOTRACE_LLM_{role.upper()}_MAX_CONCURRENT", "3")))
        except (TypeError, ValueError) as exc:
            raise RedisCallGateError(
                "invalid Redis model-call limit configuration",
                error_code="redis_configuration_error",
            ) from exc
        return redis_coordinator.execute(
            send,
            provider=provider_id,
            credential=credential,
            timeout_seconds=timeout_seconds,
            request_cost=token_cost,
            request_limit=request_limit,
            token_limit=token_limit,
            scopes=(("provider", provider_id, provider_limit), ("role", role, role_limit)),
        )

    return transport_guard


def _llm_extra_body() -> dict[str, Any] | None:
    raw = os.getenv("ECHOTRACE_LLM_EXTRA_BODY_JSON", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _search_documents(payload: dict[str, Any]) -> list[Document]:
    """Convert a search Tool envelope into durable source documents."""

    documents: list[Document] = []
    for record in payload.get("documents", ()):
        if not isinstance(record, dict):
            continue
        content = str(record.get("content") or record.get("snippet") or "").strip()
        url = str(record.get("url") or "").strip() or None
        if not content or not url:
            continue
        title = str(record.get("title") or record.get("name") or url).strip()
        provider = str(record.get("source") or "search_web").strip() or "search_web"
        durable_source = provider if provider in {
            "official", "official_announcement", "news", "media", "user_feedback",
            "community", "rss", "public_page", "web_search",
        } else "web_search"
        metadata = dict(record.get("metadata") or {})
        metadata.setdefault("provider", provider)
        metadata.setdefault("_source_label", provider)
        if record.get("domain"):
            metadata.setdefault("domain", record["domain"])
        documents.append(Document(
            source=durable_source,
            title=title,
            content=content,
            url=url,
            published_at=_parse_datetime(record.get("published_at") or record.get("published_date")),
            fetched_at=_parse_datetime(record.get("fetched_at")) or beijing_now(),
            author=str(record.get("author")).strip() if record.get("author") else None,
            content_hash=str(record.get("content_hash") or ""),
            document_id=str(record.get("document_id") or "") or new_id("doc"),
            metadata=metadata,
        ))
    return documents


def _document_payload(document: Document) -> dict[str, Any]:
    metadata = dict(document.metadata or {})
    if not document.url:
        metadata.setdefault("external_id", f"supplied:{document.document_id}")
    return {
        "document_id": document.document_id,
        "source": document.source,
        "title": document.title,
        "content": document.content,
        "url": document.url,
        "content_hash": document.content_hash,
        "metadata": metadata,
    }


def _source_resource_descriptor(document: dict[str, Any]) -> dict[str, Any]:
    """Create the bounded source card exposed by the core source tools."""

    document_id = str(document.get("document_id") or "").strip()
    title = str(document.get("title") or document_id)[:240]
    content = " ".join(str(document.get("content") or document.get("snippet") or "").split())
    summary = str(document.get("summary") or content[:480]).strip()[:480]
    locator = {"url": str(document.get("url") or "")}
    return {
        "resource_id": f"document:{document_id}",
        "resource_kind": "evidence",
        "title": title,
        "summary": summary,
        "why_relevant": "用于当前节点主张的来源核对和补充检索。",
        "when_to_use": ["需要确认主体、行动、结果、时间或限定条件"],
        "when_not_to_use": ["只需要来源数量或发布时间统计"],
        "citation": {"document_id": document_id, "locator": locator, "content_hash": str(document.get("content_hash") or "")},
        "available_representations": ["card", "excerpt", "normalized_chunk"],
        "access": {"capability": "get_source_document", "argument_hint": {"document_id": document_id}},
        "limits": {"max_chars": 12000, "requires_permission": False},
        "omitted": False,
    }


def _validate_orchestrator_tasks(parsed: dict[str, Any], context: dict[str, Any]) -> None:
    expected_top_level = {"template_id", "template_version", "tasks"}
    actual_top_level = set(parsed)
    if actual_top_level != expected_top_level:
        missing = sorted(expected_top_level - actual_top_level)
        extra = sorted(actual_top_level - expected_top_level)
        raise TraceContractError(
            "Orchestrator business JSON must contain exactly template_id, template_version and tasks"
            f" (missing={missing}, extra={extra})"
        )
    template = context.get("trace_template")
    if not isinstance(template, dict):
        raise TraceContractError("Orchestrator context is missing its pinned trace_template")
    if (parsed.get("template_id"), parsed.get("template_version")) != (
        template.get("template_id"), template.get("template_version"),
    ):
        raise TraceContractError("Orchestrator output must preserve the pinned template identity")

    tasks = parsed.get("tasks")
    if not isinstance(tasks, list):
        raise TraceContractError("Orchestrator tasks 必须是数组")

    targets = context.get("node_targets")
    if not isinstance(targets, list):
        raise TraceContractError("Orchestrator 上下文缺少 node_targets 数组")
    expected = {
        str(item.get("slot_id"))
        for item in targets
        if isinstance(item, dict) and isinstance(item.get("slot_id"), str)
    }
    if len(expected) != len(targets):
        raise TraceContractError("Orchestrator 上下文包含无效或重复的 node_targets.slot_id")

    available = context.get("available_capabilities")
    if not isinstance(available, (list, tuple)) or not available:
        raise TraceContractError("Orchestrator context is missing available_capabilities")
    available_capabilities = set(available)
    dependencies = {
        str(item.get("slot_id")): set(item.get("depends_on_slot_ids") or ())
        for item in template.get("node_specs", [])
        if isinstance(item, dict) and isinstance(item.get("slot_id"), str)
    }

    proposed: list[str] = []
    invalid_entries: list[str] = []
    for index, item in enumerate(tasks):
        if isinstance(item, dict):
            required_fields = {"slot_id", "question", "capability", "priority"}
            allowed_fields = required_fields | {"depends_on_slot_ids"}
            missing_fields = required_fields - set(item)
            extra_fields = set(item) - allowed_fields
            if missing_fields:
                invalid_entries.append(f"tasks[{index}] missing fields={sorted(missing_fields)}")
            if extra_fields:
                invalid_entries.append(f"tasks[{index}] unsupported fields={sorted(extra_fields)}")
            question = item.get("question")
            if not isinstance(question, str) or not question.strip():
                invalid_entries.append(f"tasks[{index}].question must be a non-empty string")
            capability = item.get("capability")
            if not isinstance(capability, str) or capability not in available_capabilities:
                invalid_entries.append(f"tasks[{index}].capability must be available")
            task_dependencies = item.get("depends_on_slot_ids", [])
            slot_value = item.get("slot_id")
            if not isinstance(task_dependencies, list) or any(not isinstance(value, str) for value in task_dependencies):
                invalid_entries.append(f"tasks[{index}].depends_on_slot_ids must be an array of slot IDs")
            elif len(task_dependencies) != len(set(task_dependencies)):
                invalid_entries.append(f"tasks[{index}].depends_on_slot_ids cannot contain duplicates")
            elif any(value not in expected or value == slot_value for value in task_dependencies):
                invalid_entries.append(f"tasks[{index}].depends_on_slot_ids contains unknown or self slot")
            elif isinstance(slot_value, str):
                dependencies.setdefault(slot_value, set()).update(task_dependencies)
        if not isinstance(item, dict):
            invalid_entries.append(f"tasks[{index}] 不是对象")
            continue
        slot_id = item.get("slot_id")
        if not isinstance(slot_id, str) or not slot_id.strip():
            invalid_entries.append(f"tasks[{index}].slot_id 不是非空字符串")
        else:
            proposed.append(slot_id)
        priority = item.get("priority")
        if type(priority) is not int or priority < 0:
            invalid_entries.append(f"tasks[{index}].priority 必须是非负整数")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for slot_id in proposed:
        if slot_id in seen:
            duplicates.add(slot_id)
        seen.add(slot_id)
    missing = expected - seen
    unassigned = seen - expected
    problems = invalid_entries[:]
    if duplicates:
        problems.append("重复 slot_id=" + ", ".join(sorted(duplicates)))
    if missing:
        problems.append("遗漏 slot_id=" + ", ".join(sorted(missing)))
    if unassigned:
        problems.append("未分配 slot_id=" + ", ".join(sorted(unassigned)))
    if len(proposed) != len(expected) and not (duplicates or missing or unassigned):
        problems.append("tasks 数量必须与 node_targets 完全一致")
    if problems:
        raise TraceContractError("Orchestrator 任务契约错误：" + "；".join(problems))


    remaining = {slot: deps for slot, deps in dependencies.items()}
    resolved: set[str] = set()
    while remaining:
        ready = [slot for slot, deps in remaining.items() if deps.issubset(resolved)]
        if not ready:
            raise TraceContractError("Orchestrator task dependencies contain a cycle")
        for slot in ready:
            resolved.add(slot)
            remaining.pop(slot)


def _validate_orchestrator_classification(parsed: dict[str, Any], context: dict[str, Any]) -> None:
    """Validate the business contract used when the orchestrator classifies a run."""

    if set(parsed) != {"investigation_type", "rationale"}:
        raise TraceContractError(
            "Orchestrator classification must contain exactly investigation_type and rationale"
        )
    candidates = context.get("investigation_type_candidates")
    if not isinstance(candidates, (list, tuple)) or not candidates:
        raise TraceContractError("Orchestrator classification context is missing investigation_type_candidates")
    allowed = {
        str(item.get("investigation_type") or "").strip()
        for item in candidates
        if isinstance(item, dict) and str(item.get("investigation_type") or "").strip()
    }
    selected = parsed.get("investigation_type")
    if not isinstance(selected, str) or selected.strip() not in allowed:
        raise TraceContractError(
            f"Orchestrator classification selected unsupported type: {str(selected or '').strip() or '(empty)'}"
        )
    rationale = parsed.get("rationale")
    if not isinstance(rationale, str) or not " ".join(rationale.split()).strip():
        raise TraceContractError("Orchestrator classification rationale must be non-empty")
    if len(" ".join(rationale.split())) > 500:
        raise TraceContractError("Orchestrator classification rationale is too long")


def _build_source_collector(router: ToolRouter):
    def collect(run: InvestigationRun) -> list[Document]:
        router.ensure_discovered()
        scope = run.scope if isinstance(run.scope, dict) else {}
        try:
            limit = max(1, min(int(scope.get("source_limit", 10)), 20))
        except (TypeError, ValueError):
            limit = 10
        query = run.objective.strip()
        if run.subject and run.subject not in query:
            query = f"{run.subject} {query}"
        payload: dict[str, Any] = {"query": query, "limit": limit}
        domains = scope.get("domains")
        if isinstance(domains, list) and domains:
            payload["domains"] = [str(item).strip() for item in domains if str(item).strip()]
        return _search_documents(router.call("researcher", "search_web", payload, run=run))

    return collect


_NODE_PLANNING_CAPABILITIES = frozenset({
    "search_web", "fetch_public_page", "ingest_public_feed", "analyze_discourse",
})


def _available_node_capabilities(router: ToolRouter) -> list[str]:
    """Return configured research capabilities that node tasks may declare."""

    router.ensure_discovered()
    context = router.tool_context("researcher")
    return sorted({
        str(item["capability"])
        for key in ("core_tools", "deferred_tools")
        for item in context.get(key, [])
        if isinstance(item, dict)
        and item.get("capability") in _NODE_PLANNING_CAPABILITIES
        and item.get("available") is True
    })


def _build_node_controller(
    router: ToolRouter,
    skills: AgentSkillCatalog,
    store: Any,
    node_store: Any,
    *,
    plan_loader: Any | None = None,
    plan_saver: Any | None = None,
    repository_factory: Any | None = None,
    redis_coordinator: RedisCallCoordinator | None = None,
    response_cache: RedisResponseCache | None = None,
    available_capabilities: list[str] | tuple[str, ...] | None = None,
) -> NodeInvestigationController:
    router.ensure_discovered()
    node_capabilities = list(
        available_capabilities if available_capabilities is not None
        else _available_node_capabilities(router)
    )
    base_url = os.getenv("ECHOTRACE_LLM_BASE_URL", "").strip()
    api_key = os.getenv("ECHOTRACE_LLM_API_KEY", "").strip()
    model_name = os.getenv("ECHOTRACE_LLM_MODEL", "").strip()
    if not (base_url and api_key and model_name):
        raise RuntimeError(
            "live Agent is not configured; set ECHOTRACE_LLM_BASE_URL, "
            "ECHOTRACE_LLM_API_KEY, and ECHOTRACE_LLM_MODEL"
        )

    bound_run: ContextVar[InvestigationRun | None] = ContextVar("echotrace_bound_run", default=None)
    document_persist_lock = RLock()

    class PersistingToolGateway:
        """Expose the router while durably attaching newly found sources."""

        _SOURCE_CAPABILITIES = (
            "list_source_resources", "get_source_excerpt", "get_source_document",
            "get_analysis_context", "get_transcript_context",
        )

        def __init__(self, run_id: str, context: dict[str, Any] | None = None):
            self.run_id = run_id
            self.context = context or {}
            self.source_documents = self.context.get("source_documents")
            self.task_tool_calls = 0
            self.task_search_calls = 0

        def ensure_discovered(self):
            return router.ensure_discovered()

        def tool_context(self, *args, **kwargs):
            value = router.tool_context(*args, **kwargs)
            agent = args[0] if args else kwargs.get("agent")
            if agent == AgentRole.CONCLUSION_FORMATTER.value:
                return {**value, "core_tools": [], "deferred_tools": [], "loaded_tools": []}
            capabilities = args[1] if len(args) > 1 else kwargs.get("capabilities")
            allowed = None if capabilities is None else set(capabilities)
            if agent in {"researcher", "verifier"}:
                known = {item.get("capability") for item in value.get("core_tools", [])}
                for capability in self._SOURCE_CAPABILITIES:
                    if allowed is not None and capability not in allowed:
                        continue
                    if capability in known:
                        continue
                    detail = router.capability_detail(capability, agent)
                    detail["available"] = True
                    detail["exposure"] = "core"
                    value.setdefault("core_tools", []).append(detail)
            return value

        def native_tools(self, *args, **kwargs):
            value = router.native_tools(*args, **kwargs)
            agent = args[0] if args else kwargs.get("agent")
            if agent == AgentRole.CONCLUSION_FORMATTER.value:
                return []
            capabilities = args[1] if len(args) > 1 else kwargs.get("capabilities")
            allowed = None if capabilities is None else set(capabilities)
            if agent in {"researcher", "verifier"}:
                existing = {item.get("function", {}).get("name") for item in value}
                for capability in self._SOURCE_CAPABILITIES:
                    if allowed is not None and capability not in allowed:
                        continue
                    if capability in existing:
                        continue
                    detail = router.capability_detail(capability, agent)
                    value.append({
                        "type": "function",
                        "function": {
                            "name": capability,
                            "description": detail.get("description", ""),
                            "parameters": detail.get("input_schema") or {"type": "object", "properties": {}},
                            "strict": True,
                        },
                    })
            return value

        def call(self, agent: str, capability: str, payload: dict[str, Any], *, run: Any | None = None):
            if agent == AgentRole.CONCLUSION_FORMATTER.value:
                raise ToolUnavailable("conclusion_formatter cannot call business tools", "capability_denied")
            # Search and tool counts are diagnostics. Redis provider rate and
            # concurrency gates remain the shared protection boundary; a node
            # must not lose retrieval solely because a local counter elapsed.
            if capability in SEARCH_CAPABILITIES:
                self.task_search_calls += 1
            self.task_tool_calls += 1
            if capability in self._SOURCE_CAPABILITIES:
                return self._source_call(capability, payload)
            result = router.call(agent, capability, payload, run=run)
            if self.run_id and capability in {"search_web", "fetch_public_page", "ingest_public_feed", "normalize_documents"}:
                records: list[dict[str, Any]] = []
                if isinstance(result.get("documents"), list):
                    records.extend(item for item in result["documents"] if isinstance(item, dict))
                if isinstance(result.get("document"), dict):
                    records.append(result["document"])
                documents = _search_documents({"documents": records})
                if documents:
                    with document_persist_lock:
                        store.append_documents(self.run_id, documents)
                    if self.source_documents is not None:
                        known = {str(item.get("document_id")) for item in self.source_documents}
                        for document in documents:
                            if document.document_id in known:
                                continue
                            self.source_documents.append(_document_payload(document))
            return result

        def _source_call(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
            documents = [item for item in (self.source_documents or ()) if isinstance(item, dict)]
            by_id = {str(item.get("document_id")): item for item in documents if str(item.get("document_id") or "").strip()}
            limit = max(1, min(int(payload.get("limit", 12) or 12), 32))
            if capability == "list_source_resources":
                query = str(payload.get("query") or "").strip().lower()
                cache_request = {
                    "query": query,
                    "limit": limit,
                    "documents": [
                        [
                            str(item.get("document_id") or ""),
                            str(item.get("content_hash") or sha256(str(item.get("content") or "").encode("utf-8")).hexdigest()),
                        ]
                        for item in documents
                    ],
                }
                if response_cache is not None:
                    cached = response_cache.get(
                        capability="source_resource_projection",
                        provider="mysql",
                        provider_version="1",
                        schema_version="1",
                        request=cache_request,
                    )
                    if cached is not None:
                        return cached
                selected = [
                    item for item in documents
                    if not query or query in " ".join(str(item.get(key) or "") for key in ("title", "content", "url")).lower()
                ]
                resources = [_source_resource_descriptor(item) for item in selected[:limit]]
                result = {"resources": resources, "count": len(resources), "next_cursor": None, "omissions": [{"kind": "documents", "count": max(0, len(selected) - len(resources)), "reacquire": "list_source_resources"}] if len(selected) > len(resources) else []}
                if response_cache is not None:
                    response_cache.set(
                        capability="source_resource_projection",
                        provider="mysql",
                        provider_version="1",
                        schema_version="1",
                        request=cache_request,
                        response=result,
                    )
                return result
            document_id = str(payload.get("document_id") or "").strip()
            evidence_id = str(payload.get("evidence_id") or "").strip()
            if evidence_id:
                for evidence in (self.context.get("current_draft") or {}).get("evidence", ()):
                    if isinstance(evidence, dict) and str(evidence.get("evidence_id") or "") == evidence_id:
                        document_id = str(evidence.get("document_id") or document_id)
                        break
            if capability in {"get_source_excerpt", "get_source_document"}:
                document = by_id.get(document_id)
                if document is None:
                    raise RuntimeError(f"source document not found: {document_id}")
                text = str(document.get("content") or document.get("snippet") or "")
                max_chars = max(1, min(int(payload.get("max_chars", 4000) or 4000), 12000))
                excerpt = text[:max_chars]
                citation = {"document_id": document_id, "locator": {"url": str(document.get("url") or "")}, "content_hash": str(document.get("content_hash") or "")}
                if evidence_id:
                    citation["evidence_id"] = evidence_id
                return {"resource_id": f"evidence:{evidence_id}" if evidence_id else f"document:{document_id}", "resource_kind": "evidence", "excerpt" if capability == "get_source_excerpt" else "text": excerpt, "citation": citation, "content_hash": str(document.get("content_hash") or ""), "truncated": len(text) > max_chars, "next_cursor": str(max_chars) if len(text) > max_chars else None}
            if capability == "get_analysis_context":
                features = [item for item in (self.context.get("source_analysis_features") or ()) if isinstance(item, dict)]
                feature_type = str(payload.get("feature_type") or "").strip()
                if feature_type:
                    features = [item for item in features if str(item.get("feature_type") or "") == feature_type]
                return {"features": features[:limit], "count": min(len(features), limit), "coverage": {"total": len(features), "returned": min(len(features), limit), "factuality": "analysis_feature"}, "next_cursor": None}
            items = [item for item in (self.context.get("transcript") or ()) if isinstance(item, dict)][-limit:]
            return {"items": items, "count": len(items), "next_cursor": None}

    def live_runner(role: str, objective: str, context: dict[str, Any]):
        run = bound_run.get()
        task = context.get("task") or context.get("node_task") or {}
        run_id = str(context.get("run_id") or (task.get("run_id") if isinstance(task, dict) else "") or "")
        try:
            task_timeout = max(1.0, float(os.getenv("ECHOTRACE_AGENT_TASK_TIMEOUT_SECONDS", "2400")))
        except ValueError:
            task_timeout = 2400.0
        # A durable node task carries an absolute deadline.  Every contract
        # retry, Redis redelivery, and worker restart must consume the same
        # deadline instead of receiving a fresh full timeout.
        task_budget = context.get("task_budget") if isinstance(context.get("task_budget"), dict) else {}
        absolute_deadline = task_budget.get("deadline_epoch")
        try:
            remaining_budget = float(absolute_deadline) - time.time() if absolute_deadline is not None else task_timeout
        except (TypeError, ValueError):
            remaining_budget = task_timeout
        deadline = time.monotonic() + max(0.0, min(task_timeout, remaining_budget))

        def check_active() -> None:
            if not run_id:
                return
            current = store.get(run_id)
            if current is not None and current.status == InvestigationStatus.PAUSED:
                raise TaskDeferredError("investigation was paused while an Agent was running")

        thinking, thinking_max_tokens, extra_body = _llm_thinking_config()

        client = OpenAICompatibleModelClient(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            max_tokens=thinking_max_tokens if thinking else int(os.getenv("ECHOTRACE_LLM_MAX_TOKENS", "16384")),
            timeout_seconds=float(os.getenv("ECHOTRACE_LLM_TIMEOUT_SECONDS", "900")),
            transport_guard=_make_model_transport_guard(redis_coordinator, role),
            max_parallel_tool_calls=int(os.getenv("ECHOTRACE_AGENT_MAX_PARALLEL_TOOL_CALLS", "4")),
            max_retries=int(os.getenv("ECHOTRACE_LLM_MAX_RETRIES", "2")),
            extra_body=extra_body,
            deadline=deadline,
            budget_role=role,
        )

        def persist_trace(event: dict[str, Any]) -> None:
            if run is None:
                return
            append_event = getattr(store, "append_event", None)
            if callable(append_event):
                append_event(run.run_id, event)

        trace_sink = InvestigationRunTraceSink(run, persist=persist_trace) if run is not None else None

        def trace(event: dict[str, Any]) -> None:
            if trace_sink is not None:
                trace_sink.write(event)

        def activation(event: dict[str, Any]) -> None:
            if trace_sink is not None:
                trace_sink.add_trace(
                    "skill_activation", "completed", skill=event.get("name"),
                    skill_hash=event.get("content_hash"), version=event.get("version"), agent=role,
                )

        def validate_role_answer(answer: str) -> str:
            # The native control call carries the business payload as a JSON
            # string.  Models sometimes place a literal newline/tab inside a
            # nested prose field instead of escaping it for that inner JSON
            # layer.  The outer control call has already been parsed and the
            # domain validators below still enforce the complete contract, so
            # accept those common control characters at this boundary.
            parsed = json.loads(answer, strict=False)
            if not isinstance(parsed, dict):
                raise TraceContractError("角色输出必须是 JSON 对象")
            if role == AgentRole.RESEARCHER.value:
                # The presentation IR is generated by the dedicated formatter
                # after the researcher draft has passed source validation.
                parsed.pop("conclusion_ir", None)
                _prepare_researcher_contract(parsed)
                _project_researcher_claim_sources(parsed)
                task_payload = context.get("task") or context.get("node_task") or {}
                expected_identity = (
                    task_payload.get("node_id"),
                    task_payload.get("slot_id"),
                    task_payload.get("revision"),
                )
                if all(value is not None for value in expected_identity):
                    parsed.update(dict(zip(("node_id", "slot_id", "revision"), expected_identity)))
                    answer = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                # Claim/evidence IDs emitted by the model are temporary
                # business references. NodeController normalizes them and
                # validates the complete evidence graph before persistence.
                if not isinstance(parsed.get("claims"), list) or not isinstance(parsed.get("evidence"), list):
                    raise TraceContractError("NodeDraft claims and evidence must be arrays")
            elif role == AgentRole.VERIFIER.value:
                expected_round = context.get("review_round")
                if isinstance(expected_round, int):
                    if parsed.get("revision") != expected_round:
                        raise TraceContractError(
                            f"NodeReview revision must match the assigned round: expected {expected_round}"
                        )
                    expected_status = "finalize" if expected_round == 3 else "revise"
                    if parsed.get("status") != expected_status:
                        raise TraceContractError(
                            f"NodeReview status for assigned round {expected_round} must be {expected_status}"
                        )
                NodeReview.from_dict(parsed)
            elif role == AgentRole.ORCHESTRATOR.value:
                if context.get("classification") is True:
                    _validate_orchestrator_classification(parsed, context)
                else:
                    _validate_orchestrator_tasks(parsed, context)
            elif role == AgentRole.REPORT_COMPOSER.value:
                _validate_report_answer(parsed, context)
            elif role == AgentRole.CONCLUSION_FORMATTER.value:
                _validate_conclusion_formatter_answer(parsed, context)
            return answer

        configured_steps = int(os.getenv("ECHOTRACE_AGENT_MAX_STEPS", "32"))
        role_step_limit = (
            min(configured_steps, int(os.getenv("ECHOTRACE_RESEARCH_AGENT_MAX_STEPS", "16")))
            if role == AgentRole.RESEARCHER.value else configured_steps
        )
        if role == AgentRole.CONCLUSION_FORMATTER.value:
            role_step_limit = max(1, min(role_step_limit, int(os.getenv("ECHOTRACE_FORMATTER_AGENT_MAX_STEPS", "8"))))
        runtime = AgentRuntime(
            client, PersistingToolGateway(run_id, context), skills, max_steps=role_step_limit,
            prompt_registry=NODE_ROLE_PROMPTS, trace_sink=trace, activation_sink=activation,
            max_model_errors=max(0, int(os.getenv("ECHOTRACE_AGENT_MAX_MODEL_ERRORS", "2"))),
            deadline=deadline,
            validate_answer=validate_role_answer if role in {
                AgentRole.ORCHESTRATOR.value, AgentRole.RESEARCHER.value, AgentRole.VERIFIER.value,
                AgentRole.CONCLUSION_FORMATTER.value,
            } else None,
            max_contract_retries=max(0, int(os.getenv("ECHOTRACE_AGENT_CONTRACT_RETRIES", "2"))),
            active_check=check_active,
        )
        repair_detail = ""
        research_progress_state = None
        if role == AgentRole.RESEARCHER.value:
            research_progress_state = ResearchProgress.from_context(build_context_pack(role, context))
        contract_attempts = 1 + max(0, int(os.getenv("ECHOTRACE_AGENT_CONTRACT_RETRIES", "2")))
        remaining_steps = role_step_limit
        for attempt in range(contract_attempts):
            if remaining_steps <= 0:
                failure = "step_budget_exhausted"
                result = None
                break
            attempt_objective = objective
            if repair_detail:
                attempt_objective += (
                    "\n\n上一次输出没有满足该角色 JSON 契约，必须修复："
                    f"{repair_detail}。只返回角色契约要求的 JSON，不要返回 Skill 包络、解释或 Markdown。"
                )
            result = runtime.run(
                role,
                attempt_objective,
                tool_run=trace_sink,
                allowed_capabilities=[]
                if role in {AgentRole.CONCLUSION_FORMATTER.value, AgentRole.REPORT_COMPOSER.value}
                else None,
                context_pack=lambda: build_context_pack(role, context),
                max_steps=remaining_steps,
                research_progress_state=research_progress_state,
            )
            check_active()
            remaining_steps -= max(0, int(getattr(result, "steps", 0) or 0))
            failure = None if getattr(result, "status", None) == "completed" else getattr(result, "status", "model_error")
            report_phase = str(context.get("phase") or "final") if role == AgentRole.REPORT_COMPOSER.value else ""
            required = {
                AgentRole.ORCHESTRATOR.value: (
                    ("investigation_type", "rationale")
                    if context.get("classification") is True else ("tasks",)
                ),
                AgentRole.RESEARCHER.value: ("claims",),
                AgentRole.VERIFIER.value: ("status",),
                AgentRole.CONCLUSION_FORMATTER.value: ("node_id", "slot_id", "revision", "conclusion_ir"),
                AgentRole.REPORT_COMPOSER.value: (("report_plan",) if report_phase == "plan" else ("chapter_id", "blocks") if report_phase == "section" else ("report_id", "chapters")),
            }.get(role, ())
            if failure is None and role in {
                AgentRole.ORCHESTRATOR.value, AgentRole.RESEARCHER.value,
                AgentRole.VERIFIER.value, AgentRole.CONCLUSION_FORMATTER.value, AgentRole.REPORT_COMPOSER.value,
            }:
                try:
                    parsed = json.loads(str(getattr(result, "answer", "")), strict=False)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
                if role == AgentRole.REPORT_COMPOSER.value and report_phase == "plan":
                    parsed = _normalize_report_plan_payload(parsed)
                if not isinstance(parsed, dict) or not all(key in parsed for key in required):
                    failure = "invalid_role_contract"
                    if role == AgentRole.REPORT_COMPOSER.value:
                        repair_detail = (
                            "plan 阶段只返回 {report_plan:{title,subtitle,summary,chapters}}，"
                            "chapters 必须按模板顺序仅列出 chapter_id 与 focus；"
                            "section 阶段只返回 {chapter_id,summary,blocks,metrics,datasets,visualizations,media}，"
                            "不得返回完整 ReportDocument 或其他章节内容"
                        )
                elif role == AgentRole.RESEARCHER.value:
                    _prepare_researcher_contract(parsed)
                    _project_researcher_claim_sources(parsed)
                if failure is None and role == AgentRole.ORCHESTRATOR.value:
                    try:
                        if context.get("classification") is True:
                            _validate_orchestrator_classification(parsed, context)
                        else:
                            _validate_orchestrator_tasks(parsed, context)
                    except TraceContractError as exc:
                        failure = "invalid_role_contract"
                        repair_detail = str(exc)
                elif failure is None and role == AgentRole.RESEARCHER.value:
                    claims = parsed.get("claims")
                    evidence = parsed.get("evidence")
                    missing = []
                    if not isinstance(claims, list) or not claims:
                        missing.append("claims")
                    else:
                        for index, claim in enumerate(claims):
                            if not isinstance(claim, dict) or not str(claim.get("text") or "").strip():
                                missing.append(f"claims[{index}].text")
                            if not isinstance(claim, dict) or not claim.get("evidence_ids"):
                                missing.append(f"claims[{index}].evidence_ids")
                    if not isinstance(evidence, list) or not evidence:
                        missing.append("evidence")
                    available_document_ids = {
                        str(item.get("document_id"))
                        for item in context.get("source_documents", [])
                        if isinstance(item, dict) and str(item.get("document_id") or "").strip()
                    }
                    unknown_sources = sorted({
                        str(item.get("document_id"))
                        for item in evidence or []
                        if isinstance(item, dict)
                        and str(item.get("document_id") or "") not in available_document_ids
                    })
                    if unknown_sources:
                        missing.append("evidence.document_id=" + ",".join(unknown_sources[:4]))
                    if missing:
                        failure = "invalid_role_contract"
                        repair_detail = f"NodeDraft 契约问题：{', '.join(missing[:8])}；证据 document_id 只能使用 source_documents 中的真实 ID，不得使用 internal_search_log 或工具日志 ID"
                elif role == AgentRole.VERIFIER.value:
                    try:
                        expected_round = context.get("review_round")
                        if isinstance(expected_round, int):
                            if parsed.get("revision") != expected_round:
                                raise TraceContractError(
                                    f"NodeReview revision must match the assigned round: expected {expected_round}"
                                )
                            expected_status = "finalize" if expected_round == 3 else "revise"
                            if parsed.get("status") != expected_status:
                                raise TraceContractError(
                                    f"NodeReview status for assigned round {expected_round} must be {expected_status}"
                                )
                        NodeReview.from_dict(parsed)
                    except Exception as exc:
                        failure = "invalid_role_contract"
                        repair_detail = f"NodeReview 校验失败：{str(exc)[:400]}；请严格返回本轮允许的 revise/finalize 和反馈字段"
                elif role == AgentRole.REPORT_COMPOSER.value:
                    try:
                        if report_phase == "plan":
                            _validate_report_plan_answer(parsed, context)
                        elif report_phase == "section":
                            _validate_report_section_answer(parsed, context)
                            _validate_report_section_blocks(parsed, context)
                        else:
                            _validate_report_answer(parsed, context)
                        if report_phase in {"plan", "section"}:
                            # Keep the canonicalized section (for example,
                            # de-duplicated citations) for the assembler.
                            result = replace(
                                result,
                                answer=json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
                            )
                    except Exception as exc:
                        failure = "invalid_role_contract"
                        repair_detail = f"ReportDocument 校验失败：{str(exc)[:400]}；必须严格返回模板要求的中文报告 JSON"
                elif role == AgentRole.CONCLUSION_FORMATTER.value:
                    try:
                        _validate_conclusion_formatter_answer(parsed, context)
                    except Exception as exc:
                        failure = "invalid_role_contract"
                        repair_detail = f"conclusion_ir 校验失败：{str(exc)[:400]}；请清理内部字段、按语义组织标题/段落/列表/表格，不得新增或改变事实"
            if failure is None:
                return result
            if failure == "timeout":
                if trace_sink is not None:
                    trace_sink.add_trace("agent_failure", "blocked", agent=role, error_code="agent_task_timeout", reason="task deadline exceeded", model=model_name)
                error = TraceContractError(f"live Agent {role} exceeded its task deadline")
                error.retryable = False
                error.error_code = "agent_task_timeout"
                raise error
            if failure == "provider_rejected":
                error = TraceContractError(
                    f"live Agent {role} was rejected by the model provider: {getattr(result, 'answer', '')}"
                )
                error.retryable = False
                error.error_code = "provider_policy_rejected"
                raise error
            if failure in {"invalid_role_contract", "model_error"} and attempt + 1 < contract_attempts:
                if (
                    role == AgentRole.RESEARCHER.value
                    and research_progress_state is not None
                    and contract_error_requires_research(repair_detail or getattr(result, "answer", ""))
                ):
                    research_progress_state.reopen_for_research(
                        "retry_requires_fresh_evidence_review"
                        if failure == "invalid_role_contract"
                        else "model_error_retry"
                    )
                repair_detail = repair_detail or str(getattr(result, "answer", "")).strip() or (
                    f"缺少字段：{', '.join(required)}"
                    if failure == "invalid_role_contract"
                    else "上一轮模型请求未完成；重新检查已有证据，并在事实或主张不足时重新检索"
                )
                if trace_sink is not None:
                    trace_sink.add_trace("agent_contract", "retry", agent=role, error_code=failure, reason=repair_detail, attempt=attempt + 1)
                continue
            answer = " ".join(str(getattr(result, "answer", "")).split())[:240]
            if trace_sink is not None:
                trace_sink.add_trace(
                    "agent_failure", "blocked", agent=role,
                    error_code="agent_model_error" if failure == "model_error" else "live_model_contract_error",
                    reason=failure, error_summary=(repair_detail or answer or None), model=model_name,
                )
            raise TraceContractError(f"live Agent {role} did not produce a valid result: {failure}")
        raise TraceContractError(f"live Agent {role} did not produce a valid result")

    def make_runner():
        def runner(agent_role: str, objective: str, context: dict[str, Any]):
            return live_runner(agent_role, objective, context)

        runner.bind_run = lambda value: bound_run.set(value)
        return runner

    planner = NodeTaskPlanner(
        make_runner(), max_parallel_tasks=int(os.getenv("ECHOTRACE_NODE_MAX_PARALLEL", "3"))
    )
    controller = NodeController(
        repository=node_store, repository_factory=repository_factory,
        planner=planner, researcher=make_runner(), verifier=make_runner(), formatter=make_runner(),
        available_capabilities=node_capabilities,
        plan_loader=plan_loader or getattr(node_store, "load_plan", None),
        plan_saver=plan_saver or getattr(node_store, "save_plan", None),
        analysis_feature_sink=getattr(store, "save_analysis_feature", None),
        analysis_feature_loader=getattr(store, "list_analysis_features_many", None),
        policy=NodeControllerPolicy(),
    )
    report_runner = make_runner()
    workflow = NodeInvestigationController(
        controller=controller,
        run_store=store,
        node_store=node_store,
    )
    workflow.report_runner = report_runner

    return workflow


def build_investigation_runtime() -> InvestigationRuntime:
    """Build the single supported MySQL runtime."""

    mode = os.getenv("ECHOTRACE_PERSISTENCE", "mysql").strip().lower()
    if mode != "mysql":
        raise RuntimeError("ECHOTRACE_PERSISTENCE must be 'mysql'")
    url = os.getenv("ECHOTRACE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("ECHOTRACE_DATABASE_URL is required when ECHOTRACE_PERSISTENCE=mysql")
    object_store_config = ObjectStoreConfig.from_env()
    object_store = S3CompatibleObjectStore(
        endpoint_url=object_store_config.endpoint_url,
        bucket=object_store_config.bucket,
        access_key=object_store_config.access_key_id,
        secret_key=object_store_config.secret_access_key,
        region=object_store_config.region,
        max_bytes=object_store_config.max_object_bytes,
        use_tls=object_store_config.use_tls,
    )
    object_store.ensure_bucket()
    redis_config = RedisConfig.from_env()
    redis_bus = RedisStreamBus.from_config(redis_config)
    redis_coordinator = RedisCallCoordinator(
        RedisPermitManager(redis_bus.client),
        RedisRateLimiter(redis_bus.client),
    )
    response_cache = RedisResponseCache(redis_bus.client)

    def provider_call_guard(provider: str, capability: str, payload: dict[str, Any], timeout_seconds: float, operation: Any):
        credential_names = {
            "search-web-bocha": "BOCHA_WEB_SEARCH_API_KEY",
            "search-web-tavily": "TAVILY_API_KEY",
        }
        credential = os.getenv(credential_names.get(provider, "")) or provider
        try:
            request_limit = max(1, int(os.getenv("ECHOTRACE_TOOL_REQUESTS_PER_MINUTE", "300")))
            provider_limit = max(1, int(os.getenv("ECHOTRACE_TOOL_PROVIDER_MAX_CONCURRENT", "4")))
            return redis_coordinator.execute(
                operation,
                provider=f"tool:{provider}",
                credential=credential,
                timeout_seconds=timeout_seconds,
                request_limit=request_limit,
                token_limit=1_000_000,
                scopes=(("provider", provider, provider_limit),),
            )
        except RedisCallGateError as exc:
            raise ToolUnavailable(str(exc), exc.error_code) from exc

    router = ToolRouter(policy=InvestigationPolicy(), provider_call_guard=provider_call_guard)
    available_node_capabilities = _available_node_capabilities(router)
    source_collector = _build_source_collector(router)
    database = create_database(url)
    store = SqlAlchemyRunStore(database.session_factory)
    node_store = SqlAlchemyTraceNodeStore(database.session_factory)
    skills = AgentSkillCatalog(os.path.join(os.path.dirname(__file__), "..", "echotrace", "skills"))
    task_service = InvestigationTaskService(lambda: SqlAlchemyUnitOfWork(database.session_factory))
    controller = _build_node_controller(
        router, skills, store, node_store,
        plan_loader=task_service.get_node_plan,
        plan_saver=lambda plan, template: task_service.submit_node_plan(
            plan=plan, template=template,
            available_capabilities=available_node_capabilities,
        ),
        repository_factory=lambda: SqlAlchemyTraceNodeStore(database.session_factory),
        redis_coordinator=redis_coordinator,
        response_cache=response_cache,
        available_capabilities=available_node_capabilities,
    )
    controller.source_collector = source_collector
    controller.node_task_submitter = lambda plan, template: task_service.ensure_node_tasks(
        plan=plan, template=template, retry_failed=True,
    )
    controller.report_task_submitter = lambda run, trace, template: task_service.submit_report_tasks(
        run_id=run.run_id,
        report_id=f"report_{run.run_id}",
        template_id=template.template_id,
        template_version=template.version,
        chapter_ids=[chapter.chapter_id for chapter in template.chapters],
        trace_id=trace.trace_id,
        retry_failed=True,
    )
    for role in ("orchestrator", "researcher", "verifier", "report_composer"):
        redis_bus.ensure_group(role)
    dispatcher = OutboxDispatcher(
        lambda: SqlAlchemyUnitOfWork(database.session_factory),
        redis_bus,
        max_publish_attempts=int(os.getenv("ECHOTRACE_REDIS_MAX_PUBLISH_ATTEMPTS", "5")),
        lease_seconds=int(os.getenv("ECHOTRACE_REDIS_OUTBOX_LEASE_SECONDS", "60")),
    )
    report_state_lease = RedisLeaseManager(redis_bus.client)

    def _report_trace(run_id: str) -> NodeTrace:
        run = store.get(run_id)
        if run is None or not isinstance(run.graph_state.get("node_trace"), dict):
            raise RuntimeError(f"frozen trace not found for report run {run_id}")
        trace = refresh_frozen_trace(run.graph_state["node_trace"], controller.node_store)
        return NodeTrace(**trace)

    def _report_plan_handler(message):
        run = store.get(message.run_id)
        if run is None:
            raise RuntimeError(f"investigation not found: {message.run_id}")
        try:
            if run.graph_state.get("report_plan"):
                return {"output_refs": [f"report-plan:{message.node_id}"]}
            trace = _report_trace(message.run_id)
            template = load_report_template(trace.investigation_type)
            runner = getattr(controller, "report_runner")
            runner.bind_run(run)
            contextual_runner = lambda role, objective, context: runner(
                role, objective, {**context, "execution_task_id": message.task_id},
            )
            plan = plan_report(trace=trace, template=template, report_id=message.node_id, runner=contextual_runner)
            run.graph_state = dict(run.graph_state or {})
            run.graph_state["report_plan"] = plan
            run.checkpoint = "report_sections_pending"
            run.add_trace("report_plan", "completed", report_id=message.node_id)
            store.save(run)
            return {"output_refs": [f"report-plan:{message.node_id}"]}
        except Exception as exc:
            _record_report_failure(run, exc)
            raise

    def _report_section_handler(message):
        run = store.get(message.run_id)
        if run is None:
            raise RuntimeError(f"investigation not found: {message.run_id}")
        try:
            trace = _report_trace(message.run_id)
            template = load_report_template(trace.investigation_type)
            plan = dict(run.graph_state.get("report_plan") or {})
            if not plan:
                raise RuntimeError("report plan is not persisted")
            chapter = next((item for item in template.chapters if item.chapter_id == message.slot_id), None)
            if chapter is None:
                raise RuntimeError(f"report chapter not found: {message.slot_id}")
            runner = getattr(controller, "report_runner")
            runner.bind_run(run)
            contextual_runner = lambda role, objective, context: runner(
                role, objective, {**context, "execution_task_id": message.task_id},
            )
            order = next(index for index, item in enumerate(template.chapters, 1) if item.chapter_id == chapter.chapter_id)
            fallback_reason = ""
            try:
                section = generate_report_section(
                    trace=trace, template=template, report_id=message.node_id, runner=contextual_runner,
                    plan=plan, chapter=chapter, order=order,
                )
            except TraceContractError as exc:
                # The sources/limits chapter has no required data visual.  A
                # model contract failure must not make a valid frozen Trace
                # impossible to publish; emit an auditable text-only section.
                # Other chapters remain strict so invalid metrics/media cannot
                # silently enter the final report.
                if chapter.chapter_id != "sources_limits":
                    raise
                fallback_reason = " ".join(str(exc).split())[:240]
                section = build_source_limits_fallback_section(
                    trace=trace, chapter=chapter, order=order, reason=str(exc),
                )
            citation_warnings = section_citation_warnings(section)
            section.pop("_citation_warnings", None)
            lease = None
            deadline = time.monotonic() + 30
            while lease is None and time.monotonic() < deadline:
                lease = report_state_lease.acquire(
                    resource="report_state", identifier=message.run_id, ttl_seconds=30,
                )
                if lease is None:
                    time.sleep(0.05)
            if lease is None:
                raise RuntimeError("timed out waiting for the report-state write lease")
            try:
                latest = store.get(message.run_id)
                if latest is None:
                    raise RuntimeError(f"investigation not found: {message.run_id}")
                latest.graph_state = dict(latest.graph_state or {})
                sections = dict(latest.graph_state.get("report_sections") or {})
                if chapter.chapter_id in sections:
                    return {"output_refs": [f"report-section:{message.node_id}:{chapter.chapter_id}"]}
                sections[chapter.chapter_id] = section
                latest.graph_state["report_sections"] = sections
                for warning in citation_warnings:
                    latest.add_trace(
                        "report_section", "citations_normalized",
                        chapter_id=chapter.chapter_id,
                        unknown_citation_ids=warning.get("unknown_ids", []),
                    )
                if fallback_reason:
                    latest.add_trace(
                        "report_section", "fallback",
                        chapter_id=chapter.chapter_id,
                        reason="section_contract_error",
                        error=fallback_reason,
                    )
                if set(sections) == {item.chapter_id for item in template.chapters}:
                    source_ids = build_report_material_pack(trace)["source_document_ids"]
                    with database.session_factory() as session:
                        source_rows = session.scalars(
                            select(SourceDocument).where(SourceDocument.document_id.in_(source_ids))
                        ).all() if source_ids else []
                    source_catalog = [
                        {
                            "document_id": item.document_id,
                            "title": item.title,
                            "url": item.canonical_url or "",
                            "publisher": item.provider or "",
                            "published_at": item.published_at.isoformat() if item.published_at else "",
                        }
                        for item in source_rows
                    ]
                    report = assemble_report(
                        trace=trace, template=template, report_id=message.node_id, plan=plan,
                        sections=[sections[item.chapter_id] for item in template.chapters],
                        source_catalog=source_catalog,
                    )
                    latest.report_document = report.to_dict()
                    latest.graph_state["report_document"] = report.to_dict()
                    latest.checkpoint = "report_completed"
                    latest.status = InvestigationStatus.COMPLETED
                    latest.error = None
                    latest.interrupt_reason = None
                    latest.add_trace("report", "completed", report_id=report.report_id, chapter_count=len(report.chapters))
                else:
                    latest.add_trace("report_section", "completed", chapter_id=chapter.chapter_id)
                store.save(latest)
            finally:
                report_state_lease.release(lease)
            return {"output_refs": [f"report-section:{message.node_id}:{chapter.chapter_id}"]}
        except Exception as exc:
            _record_report_failure(run, exc)
            raise

    def _pause_report_run(run, exc: Exception) -> None:
        message = " ".join(str(exc).split())[:500] or exc.__class__.__name__
        latest = store.get(run.run_id)
        if latest is None:
            return
        latest.checkpoint = "report_composer_paused"
        latest.status = InvestigationStatus.PAUSED
        latest.error = message
        latest.interrupt_reason = message
        latest.add_trace("report", "paused", error=message)
        store.save(latest)

    def _record_report_failure(run, exc: Exception) -> None:
        """Keep retryable report contract failures observable without pausing the run."""

        if getattr(exc, "retryable", True) is False:
            _pause_report_run(run, exc)
            return
        message = " ".join(str(exc).split())[:500] or exc.__class__.__name__
        latest = store.get(run.run_id)
        if latest is None:
            return
        latest.add_trace("report", "retry", error=message)
        store.save(latest)

    report_worker = ReportComposerWorker(
        redis_bus, task_service, plan_handler=_report_plan_handler,
        section_handler=_report_section_handler,
        consumer=os.getenv("ECHOTRACE_REPORT_WORKER_ID", "report-composer-1"),
        max_deliveries=int(os.getenv("ECHOTRACE_REDIS_MAX_DELIVERIES", "3")),
        lease_seconds=int(os.getenv("ECHOTRACE_TASK_LEASE_SECONDS", "180")),
    )

    node_handler = NodeRoleTaskHandler(
        controller=controller.controller,
        run_store=store,
        node_store=node_store,
        task_service=task_service,
        workflow=controller,
    )
    max_deliveries = int(os.getenv("ECHOTRACE_REDIS_MAX_DELIVERIES", "3"))
    lease_seconds = int(os.getenv("ECHOTRACE_TASK_LEASE_SECONDS", "180"))
    try:
        researcher_workers = max(1, min(int(os.getenv("ECHOTRACE_RESEARCHER_WORKERS", "3")), 12))
        verifier_workers = max(1, min(int(os.getenv("ECHOTRACE_VERIFIER_WORKERS", "3")), 12))
    except ValueError:
        researcher_workers = verifier_workers = 3
    node_workers = tuple(
        StreamTaskWorker(
            redis_bus, task_service, node_handler, role=role,
            consumer=f"{role}-{index + 1}",
            max_deliveries=max_deliveries,
            lease_seconds=lease_seconds,
        )
        for role, count in (("researcher", researcher_workers), ("verifier", verifier_workers))
        for index in range(count)
    )

    def execute_run(run_id: str, execution_task_id: str) -> None:
        run = store.get(run_id)
        if run is None:
            raise RuntimeError(f"investigation not found: {run_id}")
        if str(run.status.value) in {"completed", "cancelled", "paused"}:
            return
        controller.run(run, execution_task_id=execution_task_id)

    def mark_run_failed(run_id: str, exc: Exception) -> None:
        latest = store.get(run_id)
        if latest is None or str(latest.status.value) in {"completed", "cancelled", "paused"}:
            return
        message = " ".join(str(exc).split())[:500] or exc.__class__.__name__
        latest.status = InvestigationStatus.FAILED
        latest.error = message
        latest.interrupt_reason = message
        latest.add_trace("run", "failed", error=message)
        store.save(latest)

    orchestrator_worker = OrchestratorRunWorker(
        redis_bus,
        execute_run,
        consumer=os.getenv("ECHOTRACE_WORKER_ID", "orchestrator-1"),
        max_deliveries=int(os.getenv("ECHOTRACE_REDIS_MAX_DELIVERIES", "3")),
        on_terminal_failure=mark_run_failed,
    )
    return InvestigationRuntime(
        run_store=store,
        controller=controller,
        task_service=task_service,
        redis_bus=redis_bus,
        dispatcher=dispatcher,
        orchestrator_worker=orchestrator_worker,
        report_worker=report_worker,
        node_workers=node_workers,
        database=database,
        object_store=object_store,
    )


def _validate_report_answer(payload: dict[str, Any], context: dict[str, Any]) -> None:
    """Validate the report role's top-level JSON before it leaves the runtime."""

    report = ReportDocument.from_dict(payload)
    expected = {
        "report_id": str(context.get("report_id") or ""),
        "run_id": str(context.get("run_id") or ""),
        "trace_id": str(context.get("trace_id") or ""),
        "investigation_type": str(context.get("investigation_type") or ""),
    }
    actual = {
        "report_id": report.report_id,
        "run_id": report.run_id,
        "trace_id": report.trace_id,
        "investigation_type": report.investigation_type,
    }
    mismatches = [key for key, value in expected.items() if value and actual[key] != value]
    if mismatches:
        raise TraceContractError("报告身份字段不匹配：" + ", ".join(mismatches))
    template = context.get("report_template")
    if isinstance(template, dict):
        if report.template_id != str(template.get("template_id") or ""):
            raise TraceContractError("报告 template_id 与指定 Markdown 模板不一致")
        if report.template_version != str(template.get("version") or ""):
            raise TraceContractError("报告 template_version 与指定 Markdown 模板不一致")
        expected_hash = str(template.get("content_hash") or "")
        if expected_hash and report.template_content_hash != expected_hash:
            raise TraceContractError("报告 template_content_hash 与指定 Markdown 模板不一致")
        expected_chapters = [str(item.get("chapter_id") or item.get("id") or "") for item in template.get("chapters") or () if isinstance(item, dict)]
        if [item.chapter_id for item in report.chapters] != expected_chapters:
            raise TraceContractError("报告章节顺序必须与指定 Markdown 模板一致")


def _validate_conclusion_formatter_answer(payload: dict[str, Any], context: dict[str, Any]) -> None:
    """Validate the presentation-only IR returned by the formatter role."""

    if set(payload) != {"node_id", "slot_id", "revision", "conclusion_ir"}:
        raise TraceContractError(
            "conclusion_formatter must return exactly node_id, slot_id, revision and conclusion_ir"
        )
    task = context.get("task") or context.get("node_task") or {}
    expected = (
        str(task.get("node_id") or ""),
        str(task.get("slot_id") or ""),
        task.get("revision"),
    )
    actual = (str(payload.get("node_id") or ""), str(payload.get("slot_id") or ""), payload.get("revision"))
    if expected[0] and actual != expected:
        raise TraceContractError("conclusion_formatter identity must match the assigned node task")
    ir_payload = payload.get("conclusion_ir") or {}
    if isinstance(ir_payload, dict):
        if "source_hash" in ir_payload:
            raise TraceContractError("conclusion_ir.source_hash is application-managed and must not be generated by the agent")
        if any(isinstance(block, dict) and "source_fragment" in block for block in ir_payload.get("blocks") or ()):
            raise TraceContractError("conclusion_ir blocks must not contain legacy source_fragment fields")
    conclusion_ir = ConclusionIR.from_dict(ir_payload)
    if "conclusion" in context:
        validate_conclusion_ir_content(str(context.get("conclusion") or ""), conclusion_ir)


def _normalize_report_plan_payload(payload: Any) -> Any:
    """Accept either the documented report_plan envelope or a bare plan."""

    if (
        isinstance(payload, dict)
        and not isinstance(payload.get("report_plan"), dict)
        and all(key in payload for key in ("title", "summary", "chapters"))
    ):
        return {"report_plan": payload}
    return payload


def _validate_report_plan_answer(payload: dict[str, Any], context: dict[str, Any]) -> None:
    plan = payload.get("report_plan") if isinstance(payload.get("report_plan"), dict) else payload
    if not isinstance(plan, dict) or not str(plan.get("title") or "").strip() or not str(plan.get("summary") or "").strip():
        raise TraceContractError("报告规划必须包含 title、summary 和 chapters")
    expected = [str(item.get("chapter_id") or item.get("id") or "") for item in context.get("report_template", {}).get("chapters", ()) if isinstance(item, dict)]
    actual = [str(item.get("chapter_id") or item.get("id") or "") for item in plan.get("chapters", ()) if isinstance(item, dict)]
    if actual != expected:
        raise TraceContractError("报告规划章节顺序必须与指定 Markdown 模板一致")


def _validate_report_section_answer(payload: dict[str, Any], context: dict[str, Any]) -> None:
    section = payload.get("section") if isinstance(payload.get("section"), dict) else payload
    chapter = context.get("chapter") if isinstance(context.get("chapter"), dict) else {}
    expected_id = str(chapter.get("chapter_id") or "")
    if str(section.get("chapter_id") or "") != expected_id:
        raise TraceContractError("报告章节 chapter_id 与规划不一致")
    if not isinstance(section.get("blocks"), list):
        raise TraceContractError("报告章节必须包含 blocks 数组")


def _validate_report_section_blocks(payload: dict[str, Any], context: dict[str, Any]) -> None:
    """Validate supported report blocks before the worker persists a section."""

    section = payload.get("section") if isinstance(payload.get("section"), dict) else payload
    chapter = context.get("chapter") if isinstance(context.get("chapter"), dict) else {}
    blocks = section.get("blocks") if isinstance(section, dict) else None
    if not isinstance(blocks, list):
        return
    # Repeated references have no semantic meaning in a reader-facing block.
    # Models commonly repeat a citation while revising a paragraph; normalize
    # that harmless duplication at the contract boundary instead of spending
    # the whole task retry budget on an otherwise valid chapter.
    normalized_blocks: list[Any] = []
    for item in blocks:
        if not isinstance(item, dict):
            normalized_blocks.append(item)
            continue
        for field in ("citations", "dataset_ids"):
            values = item.get(field)
            if isinstance(values, list):
                item[field] = list(dict.fromkeys(str(value) for value in values if str(value).strip()))
        # Claim summary tables are grounded by block citations, not datasets.
        if (
            item.get("type") == "table"
            and not item.get("dataset_ids")
            and isinstance(item.get("payload"), dict)
            and isinstance(item["payload"].get("rows") or item["payload"].get("items"), list)
            and (item["payload"].get("rows") or item["payload"].get("items"))
        ):
            item["type"] = "claim_table"
        item = _normalize_unbound_report_block(item)
        if item is not None:
            normalized_blocks.append(item)
    section["blocks"] = blocks = normalized_blocks
    allowed = set(REPORT_BLOCK_TYPES)
    invalid: list[str] = []
    for index, item in enumerate(blocks):
        if not isinstance(item, dict):
            invalid.append(f"block[{index}] must be an object (got {type(item).__name__})")
            continue
        block_type = str(item.get("type") or "")
        if block_type not in allowed:
            invalid.append(f"block[{index}] has unsupported type {block_type!r}")
    if invalid:
        chapter_id = str(chapter.get("chapter_id") or "")
        raise TraceContractError(
            f"Report section {chapter_id} has invalid blocks: {invalid}; allowed types are {sorted(allowed)}"
        )
    for index, item in enumerate(blocks):
        try:
            ReportBlock.from_dict(item)
        except Exception as exc:
            chapter_id = str(chapter.get("chapter_id") or "")
            raise TraceContractError(
                f"Report section {chapter_id} block[{index}] is invalid: {exc}"
            ) from exc


def _normalize_unbound_report_block(item: dict[str, Any]) -> dict[str, Any] | None:
    """Keep useful content when a visual block has no backing dataset."""

    block_type = str(item.get("type") or "")
    if block_type not in {"chart", "metric_grid", "timeline"} or item.get("dataset_ids"):
        return item
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    title = str(payload.get("title") or "").strip()
    citations = list(item.get("citations") or ())

    if block_type == "timeline":
        entries = payload.get("items") or payload.get("rows")
        if isinstance(entries, list) and entries:
            text_entries = [_timeline_entry_text(entry) for entry in entries]
            if all(text_entries):
                return {
                    **item,
                    "type": "ordered_list",
                    "payload": {"title": title, "items": text_entries},
                    "dataset_ids": [],
                }
            if all(isinstance(entry, dict) for entry in entries):
                return {
                    **item,
                    "type": "claim_table",
                    "payload": {"title": title, "rows": entries, "columns": payload.get("columns") or []},
                    "dataset_ids": [],
                }

    if block_type == "metric_grid":
        entries = payload.get("items") or payload.get("metrics")
        if isinstance(entries, list) and entries:
            return {
                **item,
                "type": "claim_table",
                "payload": {"title": title, "rows": entries},
                "dataset_ids": [],
            }

    if block_type == "chart":
        rows = payload.get("rows") or payload.get("items")
        if isinstance(rows, list) and rows:
            return {
                **item,
                "type": "claim_table",
                "payload": {"title": title, "rows": rows, "columns": payload.get("columns") or []},
                "dataset_ids": [],
            }
        text = next((str(payload.get(key) or "").strip() for key in ("text", "summary", "description", "caption") if str(payload.get(key) or "").strip()), "")
        if text:
            return {
                **item,
                "type": "paragraph",
                "payload": {"title": title, "text": text},
                "dataset_ids": [],
            }
        return None

    return {
        **item,
        "type": "paragraph",
        "payload": {"title": title, "text": str(payload.get("text") or payload.get("note") or "").strip()},
        "citations": citations,
        "dataset_ids": [],
    }


def _timeline_entry_text(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    when = next((str(entry.get(key) or "").strip() for key in ("time", "date", "timestamp", "published_at") if str(entry.get(key) or "").strip()), "")
    event = next((str(entry.get(key) or "").strip() for key in ("event", "action", "text", "title", "label", "summary") if str(entry.get(key) or "").strip()), "")
    detail = next((str(entry.get(key) or "").strip() for key in ("detail", "description") if str(entry.get(key) or "").strip()), "")
    description = "：".join(value for value in (event, detail) if value)
    if when and description:
        return f"{when}：{description}"
    return when or description


def _project_researcher_claim_sources(payload: dict[str, Any]) -> None:
    """Validate stale source projections against the claim evidence links.

    ``source_document_ids`` is derived from evidence, so a model may carry a
    projection from an earlier revision.  The Controller derives it again
    after it assigns stable IDs; role validation should check the actual
    evidence links rather than reject a stale projection first.
    """

    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in payload.get("evidence", [])
        if isinstance(item, dict) and str(item.get("evidence_id") or "").strip()
    }
    for claim in payload.get("claims", []):
        if not isinstance(claim, dict):
            continue
        source_ids = {
            str(evidence_by_id[str(evidence_id)].get("document_id"))
            for evidence_id in claim.get("evidence_ids", [])
            if str(evidence_id) in evidence_by_id
            and str(evidence_by_id[str(evidence_id)].get("document_id") or "").strip()
        }
        if source_ids:
            claim["source_document_ids"] = sorted(source_ids)


def _prepare_researcher_contract(payload: dict[str, Any]) -> None:
    """Apply the application-owned temporary link keys before strict validation.

    The model is allowed to omit claim/evidence IDs because the Controller
    assigns stable IDs after the role call.  Domain validation still needs
    those link keys, including ``evidence.claim_id``, to validate the graph.
    This preparation only creates structural placeholders and never invents
    claim text, types, sources, or evidence.
    """
    claims = payload.get("claims")
    evidence = payload.get("evidence")
    if not isinstance(claims, list) or not isinstance(evidence, list):
        return
    for index, claim in enumerate(claims):
        if isinstance(claim, dict):
            claim.setdefault("claim_id", f"model-claim-{index}")
    claim_ids = {
        str(claim.get("claim_id") or "")
        for claim in claims
        if isinstance(claim, dict) and str(claim.get("claim_id") or "").strip()
    }
    claim_by_evidence: dict[str, str] = {}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id") or "")
        evidence_refs = claim.get("evidence_ids")
        if not isinstance(evidence_refs, (list, tuple)):
            evidence_refs = []
            claim["evidence_ids"] = evidence_refs
        for evidence_id in evidence_refs:
            if claim_id and str(evidence_id).strip():
                claim_by_evidence.setdefault(str(evidence_id), claim_id)
    sole_claim_id = next(iter(claim_ids)) if len(claim_ids) == 1 else None
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            continue
        item.setdefault("evidence_id", f"model-evidence-{index}")
        evidence_id = str(item.get("evidence_id") or "")
        claim_id = str(item.get("claim_id") or "")
        if claim_id not in claim_ids:
            claim_id = claim_by_evidence.get(evidence_id) or sole_claim_id or ""
            if claim_id:
                item["claim_id"] = claim_id
        if claim_id in claim_ids:
            claim = next(claim for claim in claims if isinstance(claim, dict) and str(claim.get("claim_id")) == claim_id)
            evidence_refs = claim.setdefault("evidence_ids", [])
            if isinstance(evidence_refs, list) and evidence_id and evidence_id not in evidence_refs:
                evidence_refs.append(evidence_id)


__all__ = ["InvestigationRuntime", "build_investigation_runtime", "_search_documents"]
