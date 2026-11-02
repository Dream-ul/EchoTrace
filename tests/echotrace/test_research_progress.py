from pathlib import Path
import json
import time

from echotrace.agent import AgentRuntime, AgentSkillCatalog, ReplayModelClient
from echotrace.agent.contracts import AgentAction, AgentDecision, DecisionMode
from echotrace.agent.context_pack import build_context_pack
from echotrace.agent.model_clients import ModelDeadlineExceeded
from echotrace.agent.prompts import NODE_RESEARCHER_PROMPT
from echotrace.agent.research_progress import (
    ResearchProgress,
    contract_error_requires_research,
)
from echotrace.runtime.tool_gateway import ToolBudgetExceeded


class _Gateway:
    def __init__(self):
        self.calls = []

    def ensure_discovered(self):
        return None

    def tool_context(self, *args, **kwargs):
        return {"core_tools": [], "deferred_tools": [], "loaded_tools": [], "catalog_version": "test"}

    def native_tools(self, *args, **kwargs):
        return []

    def call(self, agent, capability, payload, *, run=None):
        self.calls.append((agent, capability, payload))
        return {"documents": [{
            "document_id": "doc-1", "title": "原始来源", "url": "https://example.test/doc-1",
            "content": "来源记录了主体采取的行动。",
        }]}


def test_research_progress_stops_after_repeated_search_without_new_sources():
    progress = ResearchProgress(required_aspects=("主体",))
    progress.observe_tool("search_web", {"query": "第一次"}, {"documents": [{"document_id": "doc-1"}]})
    progress.observe_tool("search_web", {"query": "第二次"}, {"documents": [{"document_id": "doc-1"}]})
    progress.observe_tool("search_web", {"query": "第三次"}, {"documents": [{"document_id": "doc-1"}]})

    assert progress.force_finalize is False
    assert progress.stop_reason is None
    assert progress.phase == "explore"


def test_only_source_grounding_errors_reopen_research():
    assert contract_error_requires_research("draft.evidence cannot be empty")
    assert contract_error_requires_research("evidence references unavailable source documents: doc-x")
    assert contract_error_requires_research("NodeDraft contract error: evidence.document_id=doc-x")
    assert not contract_error_requires_research("dataset contains unsupported fields: merge_strategy")
    assert not contract_error_requires_research("claim.evidence_ids cannot be empty")


def test_revision_three_is_read_only_and_runtime_accepts_checkpoint():
    revision_three = ResearchProgress.from_context({
        "task": {"revision": 3},
        "node_spec": {"aspects": ["主体"]},
        "research_policy": {"max_search_calls": 0, "max_tool_calls": 4},
    })
    assert revision_three.finalize_only is False
    assert revision_three.force_finalize is False
    assert revision_three.to_dict()["phase"] == "targeted"

    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "call_tool", "summary": "检索来源", "capability": "search_web", "arguments": {"query": "事件 原始来源"}},
        {"action": "research_checkpoint", "summary": "来源已足够", "answer": json.dumps({
            "status": "ready", "covered_aspects": ["主体"], "reason": "已有可定位来源",
        }, ensure_ascii=False)},
        {"action": "final", "summary": "交付节点", "answer": "{\"claims\":[]}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=6,
    )
    result = runtime.run("researcher", "填写节点", context_pack={
        "pack_version": "test",
        "run_id": "run-1",
        "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin", "node_spec": {"aspects": ["主体"]}},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_policy": {"max_search_calls": 4, "max_tool_calls": 8, "no_progress_limit": 2},
    })

    assert result.status == "completed"
    assert len(gateway.calls) == 1
    assert any(item.get("action") == "research_checkpoint" for item in result.trace)
    assert any("research_progress" in message.get("content", "") for message in model.messages[-1])


def test_revision_three_can_search_for_a_concrete_evidence_gap():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "call_tool", "summary": "补充原始来源", "capability": "search_web", "arguments": {"query": "第三轮证据缺口"}},
        {"action": "final", "summary": "交付补充后的节点", "answer": "{\"claims\":[],\"evidence\":[]}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=3,
    )
    result = runtime.run("researcher", "补充第三轮节点", context_pack={
        "task": {"revision": 3, "node_id": "node-1", "slot_id": "origin"},
        "node_spec": {"aspects": ["证据"]},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_policy": {},
    })

    assert result.status == "completed"
    assert len(gateway.calls) == 1


def test_research_progress_accepts_compacted_pack_with_null_optional_sections():
    pack = build_context_pack("researcher", {
        "task": {"revision": 0, "research_policy": None, "node_spec": None},
        "review_feedback": None,
    })
    assert pack["state"]["review_feedback"] is None
    progress = ResearchProgress.from_context(pack)

    assert progress.required_aspects == ()
    assert progress.targeted_tasks == ()
    assert "max_search_calls" not in progress.to_dict()
    assert progress.force_finalize is False


def test_ready_checkpoint_requires_all_fixed_aspects_and_review_tasks():
    progress = ResearchProgress(
        required_aspects=("主体", "结果"),
        targeted_tasks=("检索后续公告",),
    )

    accepted, detail = progress.accept_checkpoint({
        "status": "ready",
        "covered_aspects": ["主体", "结果"],
    })
    assert accepted is False
    assert detail["error_code"] == "incomplete_review_tasks"

    accepted, detail = progress.accept_checkpoint({
        "status": "ready",
        "covered_aspects": ["主体", "结果"],
        "completed_tasks": ["检索后续公告"],
    })
    assert accepted is True
    assert detail["status"] == "ready"
    assert detail["forced"] is False
    assert progress.finalize_only is True
    assert progress.force_finalize is False


def test_ready_checkpoint_rejects_uncovered_aspect_and_repeated_query():
    progress = ResearchProgress(required_aspects=("主体",))
    progress.observe_tool("search_web", {"query": "事件 官方通报"}, {"documents": [{"document_id": "doc-1"}]})

    accepted, detail = progress.accept_checkpoint({
        "status": "ready",
        "covered_aspects": [],
    })
    assert accepted is False
    assert detail["error_code"] == "uncovered_required_aspects"

    accepted, detail = progress.accept_checkpoint({
        "status": "continue",
        "next_queries": ["事件 官方通报"],
    })
    assert accepted is False
    assert detail["error_code"] == "repeated_research_query"


def test_planned_queries_are_guidance_and_allow_targeted_refinement():
    progress = ResearchProgress(
        required_aspects=("主体",),
        targeted_tasks=("核对官方通报",),
        planned_queries={"武汉大学 官方通报"},
    )

    # The Researcher can refine a reviewer suggestion after inspecting the
    # available material; only an exact repeated query is rejected.
    assert progress.admit_query("武汉大学 官方通报 2025") is True
    assert progress.admit_query("武汉大学 官方通报 2025") is False
    assert progress.admit_query("武汉大学 官方通报") is True


def test_runtime_keeps_researching_after_incomplete_ready_checkpoint():
    gateway = _Gateway()
    model = ReplayModelClient([
        {
            "action": "research_checkpoint",
            "summary": "尝试提前结束",
            "answer": json.dumps({"status": "ready", "covered_aspects": []}, ensure_ascii=False),
        },
        {
            "action": "research_checkpoint",
            "summary": "方面已经覆盖",
            "answer": json.dumps({"status": "ready", "covered_aspects": ["主体"]}, ensure_ascii=False),
        },
        {"action": "final", "summary": "提交节点", "answer": "{\"claims\":[]}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=5,
    )
    result = runtime.run("researcher", "填写节点", context_pack={
        "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin"},
        "node_spec": {"aspects": ["主体"]},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_policy": {"max_search_calls": 4, "max_tool_calls": 8},
    })

    assert result.status == "completed"
    assert any(
        item.get("error_code") == "uncovered_required_aspects"
        for item in result.observations
    )


def test_deadline_reserve_disables_retrieval_and_allows_final_synthesis():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "final", "summary": "交付已有证据支持的草稿", "answer": "{\"claims\":[]}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=3,
        deadline=time.monotonic() + 30,
    )
    result = runtime.run("researcher", "补充节点", context_pack={
        "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin"},
        "node_spec": {"aspects": ["主体"]},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_policy": {"max_search_calls": 4, "max_tool_calls": 8},
    })

    assert result.status == "completed"
    assert gateway.calls == []
    assert not any(item.get("error_code") == "deadline_finalization_reserve" for item in result.trace)


def test_research_request_timeout_switches_to_reserved_synthesis_window():
    class DeadlineModel:
        model_name = "deadline-model"
        decision_mode = DecisionMode.JSON

        def __init__(self):
            self.deadlines = []
            self.messages = []
            self.calls = 0

        def start_session(self):
            return None

        def set_deadline(self, deadline):
            self.deadlines.append(deadline)

        def record_result(self, result):
            return None

        def decide(self, messages, *, tools=()):
            self.messages.append(messages)
            self.calls += 1
            if self.calls == 1:
                raise ModelDeadlineExceeded()
            return AgentDecision(
                action=AgentAction.FINAL,
                summary="交付已有证据支持的草稿",
                answer="{\"claims\":[]}",
            )

    model = DeadlineModel()
    overall_deadline = time.monotonic() + 60
    runtime = AgentRuntime(
        model,
        _Gateway(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=3,
        deadline=overall_deadline,
    )
    result = runtime.run("researcher", "补充节点", context_pack={
        "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin"},
        "node_spec": {"aspects": ["主体"]},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_policy": {"max_search_calls": 4, "max_tool_calls": 8},
    })

    assert result.status == "timeout"
    assert model.calls == 1
    assert not any(item.get("error_code") == "deadline_finalization_reserve" for item in result.trace)


def test_forced_finalize_rejects_continue_checkpoint():
    progress = ResearchProgress(required_aspects=("主体",))
    progress.request_finalize("search_budget_reached")

    accepted, detail = progress.accept_checkpoint({
        "status": "continue",
        "next_queries": ["新的定向检索"],
    })

    assert accepted is False
    assert detail["error_code"] == "research_finalize_only"


def test_researcher_prompt_documents_checkpoint_quality_boundary():
    instructions = NODE_RESEARCHER_PROMPT.instructions
    assert NODE_RESEARCHER_PROMPT.version == "researcher.trace-node.v7"
    assert "covered_aspects" in instructions
    assert "completed_tasks" in instructions
    assert "不要重复已经执行的查询" in instructions
    assert "第三轮仍可" in instructions
    assert "重新检索" in instructions


def test_finalize_only_rejects_tools_then_accepts_final_delivery():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "call_tool", "summary": "继续搜索", "capability": "search_web", "arguments": {"query": "事件"}},
        {"action": "final", "summary": "提交最佳草稿", "answer": "{\"claims\":[]}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=8,
    )

    result = runtime.run("researcher", "提交节点", context_pack={
        "task": {"revision": 1, "node_id": "node-1", "slot_id": "origin"},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_progress": {
            "finalize_only": True,
            "force_finalize": True,
            "stop_reason": "deadline_finalization_reserve",
        },
    })

    assert result.status == "completed"
    assert gateway.calls == []
    assert any(item.get("error_code") == "research_finalize_only" for item in result.observations)
    assert any(item.get("action") == "research_finalize_only_violation" for item in result.trace)
    assert "finalize_only=true" in model.messages[0][0]["content"]


def test_provider_budget_exhaustion_enters_finalize_only_instead_of_looping():
    class ExhaustedGateway(_Gateway):
        def call(self, agent, capability, payload, *, run=None):
            self.calls.append((agent, capability, payload))
            raise ToolBudgetExceeded(
                "Tool call budget exceeded: search-web-bocha 180",
                "provider_budget_exceeded",
            )

    gateway = ExhaustedGateway()
    model = ReplayModelClient([
        {"action": "call_tool", "summary": "补充检索", "capability": "search_web", "arguments": {"query": "新的定向查询"}},
        {"action": "final", "summary": "交付已有证据", "answer": "{\"claims\":[]}"},
    ])
    progress = ResearchProgress.from_context({
        "task": {"revision": 1, "node_id": "node-1", "slot_id": "response_treatment"},
        "state": {}, "evidence": [],
    })
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=8,
    )

    result = runtime.run(
        "researcher", "补充节点研究", context_pack={
            "task": {"revision": 1, "node_id": "node-1", "slot_id": "response_treatment"},
            "state": {}, "evidence": [], "analysis": [], "transcript": [],
        },
        research_progress_state=progress,
    )

    assert result.status == "completed"
    assert len(gateway.calls) == 1
    assert progress.finalize_only is True
    assert progress.stop_reason == "tool_provider_budget_exhausted"
    assert any(item.get("type") == "research_budget_exhausted" for item in result.observations)
    assert "finalize_only=true" in model.messages[-1][0]["content"]


def test_finalize_only_state_survives_live_runner_style_contract_retry():
    gateway = _Gateway()
    model = ReplayModelClient([
        {
            "action": "research_checkpoint",
            "summary": "已完成检索，准备综合",
            "answer": json.dumps({
                "status": "ready", "covered_aspects": ["主体"], "reason": "覆盖了节点方面",
            }, ensure_ascii=False),
        },
        {"action": "final", "summary": "草稿缺少证据", "answer": "bad draft"},
        {"action": "call_tool", "summary": "尝试重搜", "capability": "search_web", "arguments": {"query": "新的查询"}},
        {"action": "call_tool", "summary": "再次尝试重搜", "capability": "search_web", "arguments": {"query": "另一个查询"}},
    ])

    def validate(answer):
        if answer == "bad draft":
            raise ValueError("draft.evidence cannot be empty")
        return answer

    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=4,
        validate_answer=validate,
        max_contract_retries=0,
    )
    context = {
        "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin"},
        "node_spec": {"aspects": ["主体"]},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_policy": {"max_search_calls": 4, "max_tool_calls": 8},
    }
    progress = ResearchProgress.from_context(context)

    first = runtime.run(
        "researcher", "填写节点", context_pack=context,
        research_progress_state=progress,
    )
    retry = runtime.run(
        "researcher", "修复节点输出契约", context_pack=context,
        research_progress_state=progress,
    )

    assert first.status == "invalid_role_contract"
    assert retry.status == "step_limit"
    assert len(gateway.calls) == 2
    assert not any(item.get("error_code") == "research_finalize_only" for item in retry.observations)
    assert "finalize_only=true" not in model.messages[-1][0]["content"]
    assert progress.finalize_only is False


def test_structural_contract_retry_keeps_researcher_in_finalize_only_mode():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "final", "summary": "提交节点", "answer": "bad dataset"},
        {"action": "final", "summary": "已修正数据集字段", "answer": "good draft"},
    ])

    def validate(answer):
        if answer == "bad dataset":
            raise ValueError("dataset contains unsupported fields: merge_strategy")
        return answer

    progress = ResearchProgress.from_context({
        "task": {"revision": 3, "node_id": "node-1", "slot_id": "origin"},
        "research_progress": {"finalize_only": True, "force_finalize": False},
    })
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=4,
        validate_answer=validate,
        max_contract_retries=1,
    )

    result = runtime.run(
        "researcher", "修正数据字段", context_pack={
            "task": {"revision": 3, "node_id": "node-1", "slot_id": "origin"},
            "state": {}, "evidence": [], "analysis": [], "transcript": [],
            "research_progress": {"finalize_only": True, "force_finalize": False},
        },
        research_progress_state=progress,
    )

    assert result.status == "completed"
    assert gateway.calls == []
    assert progress.finalize_only is True
    assert all("finalize_only=true" in item[0]["content"] for item in model.messages)


def test_structural_contract_failure_closes_retrieval_before_repair():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "activate_skill", "summary": "读取研究规范", "skill": "research-trace-node"},
        {"action": "final", "summary": "提交结构错误的草稿", "answer": "bad draft"},
        {"action": "final", "summary": "修复草稿结构", "answer": "good draft"},
    ])

    def validate(answer):
        if answer == "bad draft":
            raise ValueError("MetricObservation.__init__() got an unexpected keyword argument 'citation_ids'")
        return answer

    progress = ResearchProgress.from_context({
        "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin"},
    })
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=5,
        validate_answer=validate,
        max_contract_retries=1,
    )

    result = runtime.run(
        "researcher", "修复节点草稿", context_pack={
            "task": {"revision": 0, "node_id": "node-1", "slot_id": "origin"},
            "state": {}, "evidence": [], "analysis": [], "transcript": [],
        }, research_progress_state=progress,
    )

    assert result.status == "completed"
    assert progress.finalize_only is True
    assert progress.stop_reason == "structural_contract_repair"
    assert gateway.calls == []
    assert "finalize_only=true" in model.messages[-1][0]["content"]


def test_contract_failure_reopens_targeted_search_before_next_final_delivery():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "final", "summary": "缺少证据", "answer": "bad draft"},
        {"action": "call_tool", "summary": "补充来源", "capability": "search_web", "arguments": {"query": "新的定向来源"}},
        {"action": "final", "summary": "提交有依据的节点", "answer": "good draft"},
    ])

    def validate(answer):
        if answer == "bad draft":
            raise ValueError("draft.evidence cannot be empty")
        return answer

    progress = ResearchProgress.from_context({
        "task": {"revision": 1, "node_id": "node-1", "slot_id": "origin"},
        "research_progress": {"finalize_only": True, "force_finalize": False},
    })
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=5,
        validate_answer=validate,
        max_contract_retries=1,
    )

    result = runtime.run(
        "researcher", "修复节点", context_pack={
            "task": {"revision": 1, "node_id": "node-1", "slot_id": "origin"},
            "state": {}, "evidence": [], "analysis": [], "transcript": [],
            "research_progress": {"finalize_only": True, "force_finalize": False},
        },
        research_progress_state=progress,
    )

    assert result.status == "completed"
    assert gateway.calls == [("researcher", "search_web", {"query": "新的定向来源"})]
    assert progress.finalize_only is False


def test_repeated_nonfinal_action_exits_finalize_only_without_waiting_for_deadline():
    gateway = _Gateway()
    model = ReplayModelClient([
        {"action": "call_tool", "summary": "继续搜索", "capability": "search_web", "arguments": {"query": "事件一"}},
        {"action": "research_checkpoint", "summary": "再试一次", "answer": "{\"status\":\"continue\",\"next_queries\":[\"事件二\"]}"},
        {"action": "final", "summary": "不应到达", "answer": "{\"claims\":[]}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=8,
    )

    result = runtime.run("researcher", "提交节点", context_pack={
        "task": {"revision": 1, "node_id": "node-1", "slot_id": "origin"},
        "state": {}, "evidence": [], "analysis": [], "transcript": [],
        "research_progress": {"finalize_only": True, "force_finalize": True},
    })

    assert result.status == "model_error"
    assert result.steps == 2
    assert gateway.calls == []


def test_finalize_only_native_tool_contract_exposes_only_final_control():
    class LeakyGateway(_Gateway):
        def tool_context(self, *args, **kwargs):
            return {
                "core_tools": [{"capability": "search_tools"}, {"capability": "get_source_document"}],
                "deferred_tools": [{"capability": "search_web"}],
                "loaded_tools": [{"capability": "get_source_excerpt"}],
                "catalog_version": "leaky-test",
            }

        def native_tools(self, *args, **kwargs):
            return [
                {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                for name in ("search_tools", "get_source_document", "search_web")
            ]

    runtime = AgentRuntime(
        ReplayModelClient([]),
        LeakyGateway(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
    )
    tools = runtime._native_tools(
        "researcher",
        {"search_web"},
        frozenset({"search_web"}),
        finalization_only=True,
    )

    assert [item["function"]["name"] for item in tools] == ["agent_control"]
    action_schema = tools[0]["function"]["parameters"]["properties"]["action"]
    assert action_schema["enum"] == ["final"]


def test_report_composer_cannot_discover_or_call_business_tools():
    class LeakyGateway(_Gateway):
        def tool_context(self, *args, **kwargs):
            return {
                "core_tools": [{"capability": "search_tools"}],
                "deferred_tools": [{"capability": "search_web"}],
                "loaded_tools": [],
                "catalog_version": "leaky-test",
            }

        def native_tools(self, *args, **kwargs):
            return [{
                "type": "function",
                "function": {"name": "search_tools", "parameters": {"type": "object"}},
            }]

    gateway = LeakyGateway()
    model = ReplayModelClient([
        {"action": "call_tool", "summary": "try discovery", "capability": "search_tools", "arguments": {"capabilities": ["search_web"]}},
        {"action": "activate_skill", "summary": "load report skill", "skill": "synthesize-investigation-report"},
        {"action": "call_tool", "summary": "try search", "capability": "search_web", "arguments": {"query": "report"}},
        {"action": "final", "summary": "deliver report", "answer": "{}"},
    ])
    runtime = AgentRuntime(
        model,
        gateway,
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        max_steps=5,
    )

    tools_before_skill = runtime._native_tools("report_composer", set())
    tools_after_skill = runtime._native_tools(
        "report_composer", set(), active_skill="synthesize-investigation-report",
    )
    assert [item["function"]["name"] for item in tools_before_skill] == ["agent_control"]
    assert [item["function"]["name"] for item in tools_after_skill] == ["agent_control"]
    assert tools_before_skill[0]["function"]["parameters"]["properties"]["action"]["enum"] == ["activate_skill"]
    assert tools_after_skill[0]["function"]["parameters"]["properties"]["action"]["enum"] == ["final", "activate_skill"]
    assert runtime._tool_context("report_composer", set())["core_tools"] == []

    result = runtime.run("report_composer", "write report")

    assert result.status == "completed"
    assert gateway.calls == []
    assert any(item.get("error_code") == "report_composer_action_not_allowed" for item in result.observations)
