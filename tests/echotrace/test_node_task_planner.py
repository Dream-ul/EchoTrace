from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from echotrace.agent import (
    AgentResult, AgentRuntime, AgentSkillCatalog, NODE_ORCHESTRATOR_PROMPT, ReplayModelClient,
)
from echotrace.domain import (
    NodeClaim, NodeDraft, NodeEvidence, NodeReview, NodeTaskPlan, TraceContractError,
    get_trace_template, list_trace_templates,
)
from echotrace.runtime import AgentRuntimeRunner, NodeTaskPlanner
from echotrace.runtime.tool_gateway import ToolRouter


CAPABILITIES = ("search_web", "fetch_public_page")


def _identities(template):
    return {spec.slot_id: f"node-{spec.order}" for spec in template.node_specs}


def _proposal(template):
    return {
        "template_id": template.template_id,
        "template_version": template.template_version,
        "tasks": [
            {"slot_id": spec.slot_id, "question": spec.goal, "capability": "search_web", "priority": spec.order}
            for spec in template.node_specs
        ],
    }


def _plan(template=None, proposal=None, **kwargs):
    template = template or get_trace_template("public_event")
    return NodeTaskPlanner(lambda *args: proposal if proposal is not None else _proposal(template)).plan(
        run_id="run-1", objective="调查公共事件的起因和处置", template=template,
        node_ids=_identities(template), available_capabilities=CAPABILITIES, **kwargs,
    )


def _revision_context(revision):
    draft = NodeDraft(
        node_id="node-5", slot_id="response_treatment", revision=revision - 1,
        claims=[NodeClaim("c1", "action", "机构发布处置公告。", source_document_ids=["d1"], evidence_ids=["e1"])],
        evidence=[NodeEvidence("e1", "c1", "d1", "机构发布处置公告。", {"url": "https://example.test/notice"})],
        conclusion="现有材料确认机构发布了处置公告，结果尚未公开。", media=[],
    )
    review = NodeReview(
        review_id=f"review-{revision}", node_id=draft.node_id, slot_id=draft.slot_id,
        revision=revision, status="finalize" if revision == 3 else "revise",
        covered_aspects=["机构行动"], missing_aspects=[] if revision == 3 else ["处置结果"],
        redundant_claims=[], unclear_claims=[], evidence_followups=[],
        rewrite_instructions=["分别说明谁采取了什么措施及目前结果。"],
        next_research_tasks=[], final_editor_note="明确来源确认的处置范围。",
    )
    return draft, review


def test_orchestrator_has_no_business_tools_or_tool_directory():
    runtime = AgentRuntime(
        ReplayModelClient([]),
        ToolRouter(),
        AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
    )

    context = runtime._tool_context("orchestrator", set())
    tools = runtime._native_tools("orchestrator", set())

    assert context["core_tools"] == []
    assert context["deferred_tools"] == []
    assert [item["function"]["name"] for item in tools] == ["agent_control"]


@pytest.mark.parametrize("template", list_trace_templates(), ids=lambda template: template.investigation_type)
def test_initial_plan_covers_every_template_slot_and_round_trips(template):
    plan = _plan(template)
    assert [task.slot_id for task in plan.tasks] == [spec.slot_id for spec in template.node_specs]
    for task, spec in zip(plan.tasks, template.node_specs, strict=True):
        assert task.revision == 0 and task.review_feedback is None
        assert task.aspects == spec.aspects and task.preferred_sources == spec.preferred_sources
        assert task.depends_on_slot_ids == spec.depends_on_slot_ids
        assert len(task.task_id) <= 36
    restored = NodeTaskPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    restored.validate(template=template, node_ids=_identities(template), available_capabilities=CAPABILITIES)
    assert restored.plan_id == plan.plan_id
    assert restored.to_dict() == plan.to_dict()


def test_plan_order_and_identity_are_stable_across_model_output_order():
    template = get_trace_template("public_event")
    proposal = _proposal(template)
    first = _plan(template, proposal)
    proposal["tasks"].reverse()
    second = _plan(template, proposal)
    assert first.plan_id == second.plan_id
    assert [task.task_id for task in first.tasks] == [task.task_id for task in second.tasks]


def test_orchestrator_receives_report_dataset_collection_needs_per_node():
    template = get_trace_template("public_event")
    observed = []

    def orchestrator(_role, _objective, context):
        observed.append(context)
        return _proposal(template)

    NodeTaskPlanner(orchestrator).plan(
        run_id="run-data-needs", objective="调查公共事件", template=template,
        node_ids=_identities(template), available_capabilities=CAPABILITIES,
    )

    targets = {item["slot_id"]: item for item in observed[0]["node_targets"]}
    spread_needs = targets["spread_amplification"]["report_data_needs"]
    assert {item["dataset_id"] for item in spread_needs} == {"event_timeline", "spread_trend"}
    assert {item["dataset_id"] for item in targets["response_treatment"]["report_data_needs"]} == {
        "event_timeline", "impact_metrics",
    }


@pytest.mark.parametrize("revision", [1, 2, 3])
def test_targeted_plan_only_contains_the_reviewed_node_and_exact_feedback(revision):
    draft, review = _revision_context(revision)
    contexts = []

    def orchestrator(role, objective, context):
        contexts.append(context)
        assert role == "orchestrator"
        proposal = _proposal(get_trace_template("public_event"))
        proposal["tasks"] = [proposal["tasks"][4]]
        return proposal

    template = get_trace_template("public_event")
    plan = NodeTaskPlanner(orchestrator).plan(
        run_id="run-1", objective="研究响应与处置", template=template,
        node_ids=_identities(template), available_capabilities=CAPABILITIES,
        current_drafts={draft.slot_id: draft}, reviews={review.slot_id: review},
        input_refs={draft.slot_id: ["d1"]},
    )
    assert len(plan.tasks) == len(contexts[0]["node_targets"]) == 1
    task = plan.tasks[0]
    assert task.review_feedback.to_dict() == review.to_dict()
    assert task.input_refs == ("d1",) and task.revision == revision
    assert task.stage == "node_revision"
    assert contexts[0]["node_targets"][0]["current_draft"] == draft.to_dict()
    assert not {"documents", "claims", "previous_gate"} & contexts[0].keys()
    assert task.task_id != _plan().tasks[4].task_id


@pytest.mark.parametrize("kind", [
    "missing_slot", "duplicate_slot", "unknown_slot", "wrong_template", "wrong_version",
    "unknown_capability", "extra_field", "global_envelope", "string_priority", "boolean_priority",
    "unknown_dependency", "self_dependency", "cycle", "wrong_task_array",
])
def test_planner_rejects_invalid_or_out_of_scope_proposals(kind):
    template = get_trace_template("public_event")
    proposal = _proposal(template)
    if kind == "missing_slot":
        proposal["tasks"].pop()
    elif kind == "duplicate_slot":
        proposal["tasks"].append(dict(proposal["tasks"][0]))
    elif kind == "unknown_slot":
        proposal["tasks"][0]["slot_id"] = "new_node"
    elif kind == "wrong_template":
        proposal["template_id"] = "official_claim.trace"
    elif kind == "wrong_version":
        proposal["template_version"] = "trace-template.v999"
    elif kind == "unknown_capability":
        proposal["tasks"][0]["capability"] = "web_search"
    elif kind == "extra_field":
        proposal["tasks"][0]["aspects"] = ["自定义方面"]
    elif kind == "global_envelope":
        proposal = {"objective": "旧任务计划", "tasks": proposal["tasks"]}
    elif kind == "string_priority":
        proposal["tasks"][0]["priority"] = "1"
    elif kind == "boolean_priority":
        proposal["tasks"][0]["priority"] = True
    elif kind == "unknown_dependency":
        proposal["tasks"][0]["depends_on_slot_ids"] = ["missing"]
    elif kind == "self_dependency":
        proposal["tasks"][0]["depends_on_slot_ids"] = ["origin_early_signals"]
    elif kind == "cycle":
        proposal["tasks"][0]["depends_on_slot_ids"] = ["event_scope"]
    elif kind == "wrong_task_array":
        proposal["tasks"] = {}
    with pytest.raises(TraceContractError):
        _plan(template, proposal)


def test_model_can_add_a_valid_dependency_without_removing_mandatory_dependencies():
    template = get_trace_template("public_event")
    proposal = _proposal(template)
    proposal["tasks"][1]["depends_on_slot_ids"] = ["spread_amplification"]
    plan = _plan(template, proposal)
    assert set(plan.tasks[1].depends_on_slot_ids) == {"origin_early_signals", "spread_amplification"}
    bad = replace(plan, tasks=tuple(
        replace(task, depends_on_slot_ids=()) if task.slot_id == "event_scope" else task
        for task in plan.tasks
    ))
    with pytest.raises(TraceContractError, match="remove a template dependency"):
        bad.validate(template=template, node_ids=_identities(template), available_capabilities=CAPABILITIES)


def test_ready_wave_waits_for_frozen_dependencies_and_limits_parallel_slots():
    plan = _plan()
    assert {task.slot_id for task in plan.ready_tasks(frozen_slot_ids=set())} == {"origin_early_signals", "spread_amplification"}
    ready = plan.ready_tasks(frozen_slot_ids={"origin_early_signals"}, active_slot_ids={"spread_amplification"})
    assert [task.slot_id for task in ready] == ["event_scope"]
    assert not plan.ready_tasks(frozen_slot_ids=set(), active_slot_ids={"origin_early_signals", "spread_amplification", "event_scope"})
    assert not plan.ready_tasks(frozen_slot_ids={task.slot_id for task in plan.tasks})


@pytest.mark.parametrize("fault", ["wrong_node", "wrong_round", "missing_draft", "unknown_slot"])
def test_invalid_revision_context_is_rejected_before_calling_the_model(fault):
    draft, review = _revision_context(1)
    drafts, reviews = {draft.slot_id: draft}, {review.slot_id: review}
    if fault == "wrong_node":
        review.node_id = "another-node"
    elif fault == "wrong_round":
        draft.revision = 1
    elif fault == "missing_draft":
        drafts.clear()
    elif fault == "unknown_slot":
        reviews = {"unknown": review}
    template = get_trace_template("public_event")
    with pytest.raises(TraceContractError):
        NodeTaskPlanner(lambda *args: pytest.fail("invalid context must not reach model")).plan(
            run_id="run-1", objective="调查", template=template,
            node_ids=_identities(template), available_capabilities=CAPABILITIES,
            current_drafts=drafts, reviews=reviews,
        )


@pytest.mark.parametrize("capabilities, refs", [("search_web", {}), ([], {}), (CAPABILITIES, {"event_scope": "d1"})])
def test_invalid_tool_or_source_context_does_not_reach_model(capabilities, refs):
    template = get_trace_template("public_event")
    with pytest.raises(TraceContractError):
        NodeTaskPlanner(lambda *args: pytest.fail("invalid context must not reach model")).plan(
            run_id="run-1", objective="调查", template=template, node_ids=_identities(template),
            available_capabilities=capabilities, input_refs=refs,
        )


@pytest.mark.parametrize("raw", ["not JSON", [], AgentResult("needs_human", "需要输入", 1, None, ())])
def test_planner_does_not_convert_model_failure_into_a_plan(raw):
    with pytest.raises(TraceContractError):
        _plan(proposal=raw)


def test_real_agent_loop_uses_node_orchestrator_prompt_with_replay_model():
    template = get_trace_template("public_event")
    model = ReplayModelClient([{
        "action": "final", "summary": "完成固定节点编排",
        "answer": json.dumps(_proposal(template), ensure_ascii=False),
    }])
    runtime = AgentRuntime(
        model, ToolRouter(), AgentSkillCatalog(Path(__file__).parents[2] / "echotrace" / "skills"),
        prompt_registry={"orchestrator": NODE_ORCHESTRATOR_PROMPT}, max_steps=1,
    )
    plan = NodeTaskPlanner(AgentRuntimeRunner(runtime)).plan(
        run_id="run-1", objective="研究公共事件", template=template,
        node_ids=_identities(template), available_capabilities=CAPABILITIES,
    )
    assert len(plan.tasks) == 6
    assert NODE_ORCHESTRATOR_PROMPT.version == "node-orchestrator.v5"
    assert "priority 必须是 0 或更大的 JSON 整数" in NODE_ORCHESTRATOR_PROMPT.instructions
    assert NODE_ORCHESTRATOR_PROMPT.instructions in model.messages[0][0]["content"]
    assert "node_targets" in model.messages[0][1]["content"]


def test_orchestrator_classification_receives_all_candidates_and_returns_registered_type():
    candidates = list_trace_templates()
    observed = []

    def orchestrator(_role, _objective, context):
        observed.append(context)
        return {"investigation_type": candidates[-1].investigation_type, "rationale": "目标与政策执行变化相关。"}

    result = NodeTaskPlanner(orchestrator).classify(
        run_id="run-classify", objective="调查政策影响", event_title="政策变化",
        subject="监管部门", source_documents=[{"document_id": "doc-1", "title": "公告", "content": "正文"}],
        candidates=candidates,
    )

    assert result["investigation_type"] == candidates[-1].investigation_type
    assert observed[0]["classification"] is True
    assert [item["investigation_type"] for item in observed[0]["investigation_type_candidates"]] == [
        item.investigation_type for item in candidates
    ]
    assert observed[0]["source_documents"][0]["document_id"] == "doc-1"


@pytest.mark.parametrize(
    "answer",
    [
        {"investigation_type": "unknown", "rationale": "无法匹配"},
        {"investigation_type": "public_event", "rationale": ""},
        {"investigation_type": "public_event", "rationale": "x" * 501},
        {"investigation_type": "public_event", "rationale": 123},
    ],
)
def test_orchestrator_classification_rejects_invalid_contract(answer):
    with pytest.raises(TraceContractError):
        NodeTaskPlanner(lambda *_args: answer).classify(
            run_id="run-classify", objective="调查", event_title="事件", subject=None,
            source_documents=[], candidates=list_trace_templates(),
        )
