from __future__ import annotations

from echotrace.domain import NodeDraft, NodeReview, NodeRevision, get_trace_template
from echotrace.domain.node_tasks import NodeTaskPlan
from echotrace.domain.trace_nodes import ConclusionIR, TraceContractError
from echotrace.runtime import NodeController, NodeControllerPolicy, NodeTaskPlanner
from echotrace.runtime.node_controller import validate_conclusion_ir_content


class MemoryNodeRepository:
    def __init__(self):
        self.nodes = {}
        self.revisions = {}
        self.reviews = {}

    def initialize_nodes(self, *, run_id, template):
        self.nodes = {
            spec.slot_id: {
                "node_id": f"node-{index}", "run_id": run_id, "slot_id": spec.slot_id,
                "status": "pending", "current_revision": 0,
            }
            for index, spec in enumerate(template.node_specs)
        }
        return list(self.nodes.values())

    def list_nodes(self, run_id):
        return [dict(row) for row in self.nodes.values()]

    def get_revision(self, node_id, revision_no=None):
        rows = [value for (candidate, _), value in self.revisions.items() if candidate == node_id]
        if revision_no is not None:
            rows = [value for value in rows if value["revision"] == revision_no]
        return max(rows, key=lambda value: value["revision"]) if rows else None

    def get_review(self, node_id, revision_no=None):
        rows = [value for (candidate, _), value in self.reviews.items() if candidate == node_id]
        if revision_no is not None:
            rows = [value for value in rows if value["review"]["revision"] == revision_no]
        return max(rows, key=lambda value: value["review"]["revision"]) if rows else None

    def save_revision(self, revision):
        key = (revision.node_id, revision.revision)
        self.revisions[key] = {"revision": revision.revision, "draft": revision.draft.to_dict()}
        self.nodes[revision.slot_id]["current_revision"] = revision.revision
        self.nodes[revision.slot_id]["status"] = "reviewing"
        return f"revision-{revision.revision}"

    def save_review(self, review):
        self.reviews[(review.node_id, review.revision)] = {"review": review.to_dict()}
        return review.review_id

    def freeze_node(self, node_id):
        row = next(value for value in self.nodes.values() if value["node_id"] == node_id)
        row["status"] = "frozen"
        return dict(row)


def test_node_role_json_boundary_accepts_literal_control_chars_in_prose():
    raw = '{"conclusion":"第一行\n第二行\t补充"}'

    parsed = NodeController._json_object(raw)

    assert parsed["conclusion"] == "第一行\n第二行\t补充"


def _proposal(template):
    return {
        "template_id": template.template_id,
        "template_version": template.template_version,
        "tasks": [
            {"slot_id": spec.slot_id, "question": spec.goal, "capability": "search_web", "priority": spec.order}
            for spec in template.node_specs
        ],
    }


def _draft(task):
    return {
        "node_id": task["node_id"], "slot_id": task["slot_id"], "revision": task["revision"],
        "claims": [{
            "claim_id": "model-claim", "claim_type": "fact", "text": "归纳后的事实主张。",
            "source_document_ids": ["doc-source"], "evidence_ids": ["model-evidence"],
        }],
        "evidence": [{
            "evidence_id": "model-evidence", "claim_id": "model-claim", "document_id": "doc-source",
            "excerpt": "来源记载了相关主体采取行动。", "locator": {"url": "https://example.test/source"},
        }],
        "conclusion": "来源显示相关主体采取了行动。", "media": [],
    }


def _controller(repository, template, *, researcher=None, verifier=None, formatter=None):
    return NodeController(
        repository=repository,
        planner=NodeTaskPlanner(lambda *_: _proposal(template)),
        researcher=researcher or (lambda _role, _objective, context: _draft(context["task"])),
        verifier=verifier or (lambda *_: None),
        formatter=formatter,
        available_capabilities=["search_web"],
        policy=NodeControllerPolicy(max_role_attempts=1),
    )


def test_model_relation_synonyms_are_rejected_by_strict_draft_contract():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    controller = _controller(repository, template)
    task = controller.prepare(run_id="run-1", objective="调查事件", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["evidence"][0]["relation"] = "qualifies"

    import pytest
    with pytest.raises(Exception, match="unsupported evidence relation"):
        controller._normalize_draft(value, task)


def test_normalizer_repairs_stale_model_evidence_reference():
    template = get_trace_template("public_event")
    controller = _controller(MemoryNodeRepository(), template)
    task = controller.prepare(run_id="run-stale-evidence", objective="调查事件", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["claims"][0]["evidence_ids"] = ["stale-evidence-id"]

    draft = controller._normalize_draft(value, task)

    assert draft.claims[0].evidence_ids == [draft.evidence[0].evidence_id]


def test_metric_source_document_is_projected_from_its_normalized_evidence():
    template = get_trace_template("public_event")
    controller = _controller(MemoryNodeRepository(), template)
    task = controller.prepare(run_id="run-stale-metric-source", objective="investigate event", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["metrics"] = [{
        "metric_id": "metric-3", "name": "reported views", "value": 100000,
        "unit": "views", "time_scope": "reported at publication",
        "source_document_id": "stale-source-document", "evidence_id": "model-evidence",
        "locator": {"url": "https://example.test/source"},
    }]

    draft = controller._normalize_draft(value, task)

    assert draft.metrics[0].source_document_id == "doc-source"
    assert draft.metrics[0].evidence_id == draft.evidence[0].evidence_id


def test_malformed_optional_derived_metric_does_not_discard_node_draft():
    template = get_trace_template("public_event")
    controller = _controller(MemoryNodeRepository(), template)
    task = controller.prepare(run_id="run-invalid-derived-metric", objective="investigate event", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["metrics"] = [{
        "metric_id": "metric-derived-without-formula", "name": "派生指标", "value": 3,
        "unit": "项", "time_scope": "调查期间", "source_document_id": "doc-source",
        "evidence_id": "model-evidence", "locator": {"url": "https://example.test/source"},
        "direct_or_derived": "derived", "formula": "",
    }]
    value["datasets"] = [{
        "dataset_id": "dataset-1", "purpose": "按指标汇总", "columns": ["value"],
        "rows": [{"value": 3}], "grain": "每个指标", "sample_scope": "当前节点",
        "time_scope": "调查期间", "method": "来源整理",
        "input_metric_ids": ["metric-derived-without-formula"], "citation_ids": ["model-evidence"],
        "limitations": [],
    }]

    draft = controller._normalize_draft(value, task)

    assert draft.metrics == []
    assert draft.datasets[0].input_metric_ids == []


def test_evidence_rejects_undeclared_source_title_field():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    controller = _controller(repository, template)
    task = controller.prepare(run_id="run-1", objective="调查事件", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["evidence"][0]["source_title"] = "无契约字段"

    import pytest
    with pytest.raises(Exception, match="evidence contains unsupported fields: source_title"):
        controller._normalize_draft(value, task)


def test_dataset_row_citations_follow_normalized_evidence_ids():
    template = get_trace_template("public_event")
    controller = _controller(MemoryNodeRepository(), template)
    task = controller.prepare(run_id="run-row-citations", objective="调查事件", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["datasets"] = [{
        "dataset_id": "timeline-1", "purpose": "逐条记录事件时间",
        "columns": ["time", "event", "subject", "citation_ids"],
        "rows": [{"time": "2026-01-01", "event": "发布通报", "subject": "机构", "citation_ids": ["model-evidence"]}],
        "grain": "每条事件", "sample_scope": "已引用来源", "time_scope": "2026年",
        "method": "按来源整理", "input_metric_ids": [], "citation_ids": ["model-evidence"],
        "limitations": [],
    }]

    draft = controller._normalize_draft(value, task)

    normalized_evidence_id = draft.evidence[0].evidence_id
    assert draft.datasets[0].citation_ids == [normalized_evidence_id]
    assert draft.datasets[0].rows[0]["citation_ids"] == [normalized_evidence_id]


def test_dataset_scalar_limitation_is_normalized_to_a_string_list():
    template = get_trace_template("public_event")
    controller = _controller(MemoryNodeRepository(), template)
    task = controller.prepare(run_id="run-dataset-limit", objective="调查事件", template=template).plan.tasks[0]
    value = _draft({"node_id": task.node_id, "slot_id": task.slot_id, "revision": 0})
    value["datasets"] = [{
        "dataset_id": "timeline-1", "purpose": "逐条记录事件时间",
        "columns": ["time", "event", "citation_ids"],
        "rows": [{"time": "2026-01-01", "event": "发布通报", "citation_ids": ["model-evidence"]}],
        "grain": "每条事件", "sample_scope": "已引用来源", "time_scope": "2026年",
        "method": "按来源整理", "input_metric_ids": [], "citation_ids": ["model-evidence"],
        "limitations": "公开材料未覆盖全部过程。",
    }]

    draft = controller._normalize_draft(value, task)

    assert draft.datasets[0].limitations == ["公开材料未覆盖全部过程。"]


def test_research_dataset_contract_error_is_retried_with_targeted_feedback():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    objectives = []

    def researcher(_role, objective, context):
        objectives.append(objective)
        value = _draft(context["task"])
        if len(objectives) == 1:
            value["datasets"] = [{
                "dataset_id": "event-timeline",
                "purpose": "event timeline",
                "columns": ["time", "event", "citation_ids"],
                "grain": "one event",
                "sample_scope": "source material",
                "time_scope": "investigation period",
                "method": "source-backed rows",
                "citation_ids": ["model-evidence"],
            }]
        return value

    controller = _controller(repository, template, researcher=researcher)
    controller.policy = NodeControllerPolicy(max_role_attempts=2)
    plan = controller.prepare(run_id="run-missing-dataset-rows", objective="investigate event", template=template).plan
    task = plan.tasks[0]

    controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[],
    )

    assert len(objectives) == 2
    assert "datasets[0].rows must be a list" in objectives[1]


def test_research_worker_persists_only_the_assigned_revision_with_stable_references():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    controller = _controller(repository, template)
    plan = controller.prepare(run_id="run-2", objective="调查事件", template=template).plan
    task = plan.tasks[0]
    source = {
        "document_id": "doc-source", "source": "official", "title": "情况说明",
        "content": "有关部门发布情况说明并采取了措施。", "url": "https://example.test/source",
        "metadata": {},
    }

    saved = controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[source],
    )

    assert saved.revision == 0
    row = repository.get_revision(task.node_id, 0)
    assert row is not None
    assert repository.get_revision(task.node_id, 1) is None
    assert row["draft"]["claims"][0]["source_document_ids"] == ["doc-source"]
    assert row["draft"]["claims"][0]["evidence_ids"] == [row["draft"]["evidence"][0]["evidence_id"]]


def test_research_worker_persists_formatter_ir_without_source_documents_or_tools():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    observed = []

    def formatter(role, _objective, context):
        observed.append((role, context))
        task = context["node_task"]
        return {
            "node_id": task["node_id"], "slot_id": task["slot_id"], "revision": task["revision"],
            "conclusion_ir": {
                "schema_version": "conclusion-ir.v1",
                "blocks": [
                    {"type": "heading", "level": 2, "text": "响应与处置", "source_fragment": "一、响应与处置"},
                    {"type": "table", "title": "主体—行动—结果—状态", "columns": ["主体", "行动", "结果", "状态"], "rows": [["机构", "发布说明", "信息公开", "已确认"]], "source_fragment": "主体—行动—结果—状态：机构｜发布说明｜信息公开｜已确认"},
                ],
            },
        }

    def researcher(_role, _objective, context):
        value = _draft(context["task"])
        value["conclusion"] = "一、响应与处置\n主体—行动—结果—状态：机构｜发布说明｜信息公开｜已确认"
        return value

    controller = _controller(repository, template, researcher=researcher, formatter=formatter)
    plan = controller.prepare(run_id="run-formatter", objective="调查事件", template=template).plan
    task = plan.tasks[0]
    saved = controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[{
            "document_id": "doc-source", "source": "official", "title": "情况说明",
            "content": "有关部门发布情况说明并采取了措施。", "url": "https://example.test/source", "metadata": {},
        }],
    )

    assert observed[0][0] == "conclusion_formatter"
    assert "source_documents" not in observed[0][1]
    assert saved.conclusion_ir is not None
    assert saved.conclusion_ir.blocks[1]["type"] == "table"
    assert saved.conclusion_ir.source_hash
    assert repository.get_revision(task.node_id, 0)["draft"]["conclusion_ir"]["schema_version"] == "conclusion-ir.v1"


def test_formatter_can_rewrite_a_conclusion_into_reader_facing_structure():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()

    def researcher(_role, _objective, context):
        value = _draft(context["task"])
        value["conclusion"] = (
            "一、响应与处置：有关部门公布了复核结果。第一，平台清理相关链接；"
            "第二，有关部门继续核查。详见 nclaim_f84ff2f31ba8aa13fdcdf067。"
        )
        return value

    def formatter(_role, _objective, context):
        task = context["node_task"]
        return {
            "node_id": task["node_id"], "slot_id": task["slot_id"], "revision": task["revision"],
            "conclusion_ir": {
                "schema_version": "conclusion-ir.v1",
                "blocks": [
                    {"type": "heading", "level": 2, "text": "响应与处置"},
                    {"type": "list", "ordered": True, "items": [
                        "平台清理相关链接。", "有关部门继续核查。",
                    ]},
                ],
            },
        }

    controller = _controller(repository, template, researcher=researcher, formatter=formatter)
    plan = controller.prepare(run_id="run-formatter-truncated", objective="调查事件", template=template).plan
    task = plan.tasks[0]

    saved = controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[],
    )
    assert saved.conclusion_ir.blocks[0] == {"type": "heading", "level": 2, "text": "响应与处置"}
    assert saved.conclusion_ir.blocks[1]["type"] == "list"
    assert "nclaim_" not in str(saved.conclusion_ir.to_dict())


def test_formatter_rejects_internal_ids_and_field_names():
    conclusion = "平台清理了相关链接。"
    ir = ConclusionIR(
        schema_version="conclusion-ir.v1",
        blocks=[{"type": "paragraph", "text": "平台清理相关链接，详见 nclaim_f84ff2f31ba8aa13fdcdf067。"}],
    )

    import pytest
    with pytest.raises(TraceContractError, match="internal identifier or field name"):
        validate_conclusion_ir_content(conclusion, ir)


def test_formatter_allows_rephrasing_and_structural_reorganization():
    conclusion = "一、甲方先说明现状。二、乙方随后回应。"
    ir = ConclusionIR(
        schema_version="conclusion-ir.v1",
        blocks=[
            {"type": "list", "ordered": True, "items": ["甲方先说明现状。", "乙方随后回应。"]},
        ],
    )

    validate_conclusion_ir_content(conclusion, ir)


def test_formatter_rejects_raw_internal_field_names():
    import pytest

    conclusion = "舆论讨论出现变化。"
    ir = ConclusionIR(
        schema_version="conclusion-ir.v1",
        blocks=[{"type": "paragraph", "text": "舆论变化 discussion_trend 与 limitations。"}],
    )

    with pytest.raises(TraceContractError, match="internal identifier or field name"):
        validate_conclusion_ir_content(conclusion, ir)


def test_researcher_receives_report_data_collection_spec_for_its_node():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    observed = []

    def researcher(_role, _objective, context):
        observed.append(context)
        return _draft(context["task"])

    controller = _controller(repository, template, researcher=researcher)
    plan = controller.prepare(run_id="run-data-needs", objective="调查事件", template=template).plan
    task = plan.tasks[0]
    controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[{
            "document_id": "doc-source", "source": "official", "title": "情况说明",
            "content": "有关部门发布情况说明。", "url": "https://example.test/source", "metadata": {},
        }],
    )

    assert {item["dataset_id"] for item in observed[0]["report_data_needs"]} == {"event_timeline"}
    assert "发生时间" in observed[0]["report_data_needs"][0]["collection_guidance"]


def test_existing_analysis_features_are_reused_without_running_analyzer(monkeypatch):
    import echotrace.runtime.node_controller as node_controller

    def unexpected_analysis(*args, **kwargs):
        raise AssertionError("complete persisted features must skip analysis")

    monkeypatch.setattr(node_controller, "analyze_document", unexpected_analysis)
    documents = [{"document_id": "doc-cached", "content": "来源内容"}]
    cached = [
        {"document_id": "doc-cached", "feature_type": feature_type, "value": {}}
        for feature_type in ("time_mentions", "entity_mentions", "sentiment", "stance", "topic")
    ]

    result = NodeController._source_analysis_features(
        documents, target="事件", existing_features=cached,
    )

    assert result == cached


def test_research_worker_binds_model_output_to_assigned_identity():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()

    def researcher(_role, _objective, context):
        value = _draft(context["task"])
        value["node_id"] = "model-copied-node"
        value["slot_id"] = "model-copied-slot"
        value["revision"] = 3
        return value

    controller = _controller(repository, template, researcher=researcher)
    plan = controller.prepare(run_id="run-identity", objective="调查事件", template=template).plan
    task = plan.tasks[0]

    controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[],
    )

    saved = repository.revisions[(task.node_id, 0)]["draft"]
    assert saved["node_id"] == task.node_id
    assert saved["slot_id"] == task.slot_id
    assert saved["revision"] == task.revision


def test_research_accepts_evidence_from_documents_discovered_during_the_task():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    discovered = {
        "document_id": "doc-discovered",
        "source": "web_search",
        "title": "补充来源",
        "content": "补充搜索返回的原始材料。",
        "url": "https://example.test/discovered",
        "metadata": {},
    }

    def researcher(_role, _objective, context):
        context["source_documents"].append(discovered)
        task = context["task"]
        return {
            "node_id": task["node_id"],
            "slot_id": task["slot_id"],
            "revision": task["revision"],
            "claims": [{
                "claim_id": "claim-discovered",
                "claim_type": "fact",
                "text": "补充搜索材料归纳出的主张。",
                "source_document_ids": ["doc-discovered"],
                "evidence_ids": ["evidence-discovered"],
            }],
            "evidence": [{
                "evidence_id": "evidence-discovered",
                "claim_id": "claim-discovered",
                "document_id": "doc-discovered",
                "excerpt": "补充搜索返回的原始材料。",
                "locator": {"url": "https://example.test/discovered"},
            }],
            "conclusion": "补充搜索材料支持该主张。",
            "media": [],
        }

    controller = _controller(repository, template, researcher=researcher)
    plan = controller.prepare(run_id="run-discovered", objective="调查事件", template=template).plan
    task = plan.tasks[0]

    saved = controller.execute_research_task(
        plan=plan, slot_id=task.slot_id, revision=0, template=template,
        source_documents=[],
    )

    assert saved.evidence[0].document_id == "doc-discovered"
    row = repository.get_revision(task.node_id, 0)
    assert row["draft"]["claims"][0]["source_document_ids"] == ["doc-discovered"]


def test_verifier_worker_persists_only_its_assigned_review_round():
    template = get_trace_template("public_event")
    repository = MemoryNodeRepository()
    observed_contexts = []

    def verifier(_role, _objective, context):
        observed_contexts.append(context)
        task = context["node_task"]
        return {
            "review_id": "model-review", "node_id": task["node_id"], "slot_id": task["slot_id"],
            "revision": context["review_round"], "status": "revise",
            "covered_aspects": ["起因"], "missing_aspects": ["处置结果"],
            "redundant_claims": [], "unclear_claims": [], "evidence_followups": ["补充独立来源"],
            "rewrite_instructions": ["说明主体采取的具体行动和结果"],
            "next_research_tasks": ["检索处置结果"], "final_editor_note": "请补充处置结果。",
        }

    controller = _controller(repository, template, verifier=verifier)
    plan = controller.prepare(run_id="run-3", objective="调查事件", template=template).plan
    task = plan.tasks[0]
    repository.save_revision(NodeRevision(task.node_id, task.slot_id, 0, NodeDraft.from_dict(_draft({
        "node_id": task.node_id, "slot_id": task.slot_id, "revision": 0,
    }))))

    review = controller.execute_review_task(
        plan=plan, slot_id=task.slot_id, review_round=1, execution_task_id="review-task-1",
        template=template, source_documents=[],
    )

    assert review.revision == 1
    assert observed_contexts[0]["run_id"] == plan.run_id
    assert review.status == "revise"
    assert repository.get_review(task.node_id, 1)["review"]["next_research_tasks"] == ["检索处置结果"]
    assert repository.get_review(task.node_id, 2) is None
