import pytest

from echotrace.domain import (
    ConclusionIR,
    EdgeSpec,
    FrozenTraceNode,
    MAX_NODE_REVISIONS,
    NodeClaim,
    NodeDraft,
    NodeEvidence,
    NodeMedia,
    NodeReview,
    NodeReviewStatus,
    NodeRevision,
    TraceContractError,
    TRACE_TEMPLATES,
    get_trace_template,
    list_trace_templates,
    review_round_context,
)


def _draft(revision: int = 0) -> NodeDraft:
    claim = NodeClaim(
        claim_id=f"claim-{revision}",
        claim_type="action",
        text="监管部门发布了处置说明。",
        source_document_ids=["doc-1"],
        evidence_ids=[f"evidence-{revision}"],
    )
    evidence = NodeEvidence(
        evidence_id=f"evidence-{revision}",
        claim_id=claim.claim_id,
        document_id="doc-1",
        excerpt="监管部门发布了处置说明。",
        locator={"quote": "监管部门发布了处置说明。"},
    )
    return NodeDraft(
        node_id="node-1",
        slot_id="response_treatment",
        revision=revision,
        claims=[claim],
        evidence=[evidence],
        conclusion="现有材料显示监管部门已经发布处置说明。",
        media=[],
    )


def test_built_in_templates_define_fixed_slots_edges_and_three_rounds():
    assert set(TRACE_TEMPLATES) == {
        "public_event",
        "public_opinion",
        "product_safety",
        "consumer_dispute",
        "official_claim",
        "policy_regulation",
    }
    assert len(list_trace_templates()) == 6
    for template in list_trace_templates():
        assert template.max_node_revisions == MAX_NODE_REVISIONS == 3
        assert [item.order for item in template.node_specs] == list(range(1, len(template.node_specs) + 1))
        slots = {item.slot_id for item in template.node_specs}
        assert slots
        assert all(edge.from_slot_id in slots and edge.to_slot_id in slots for edge in template.edge_specs)
        assert all(item.goal and item.aspects and item.claim_types and set(item.review_rubric) == {"coverage", "evidence", "synthesis"} for item in template.node_specs)
        assert get_trace_template(template.investigation_type).to_dict() == template.to_dict()


def test_review_rubric_has_distinct_round_focus_for_each_node():
    spec = get_trace_template("public_event").node_spec("response_treatment")
    focuses = [review_round_context(spec, round_no) for round_no in (1, 2, 3)]

    assert [item["axis"] for item in focuses] == ["coverage", "evidence", "synthesis"]
    assert [item["round"] for item in focuses] == [1, 2, 3]
    assert all(item["rubric"] for item in focuses)
    assert "next_research_tasks" in focuses[0]["feedback_fields"]
    assert "evidence_followups" in focuses[1]["feedback_fields"]
    assert "redundant_claims" in focuses[2]["feedback_fields"]


def test_template_round_trip_and_dependency_cycle_rejection():
    template = get_trace_template("public_event")
    restored = type(template).from_dict(template.to_dict())
    assert restored.to_dict() == template.to_dict()

    with pytest.raises(TraceContractError, match="dependencies contain a cycle"):
        type(template)(
            template_id="bad",
            template_version=template.template_version,
            investigation_type="bad",
            display_name="坏模板",
            purpose="测试",
            node_specs=(
                template.node_specs[0].__class__(
                    **{**template.node_specs[0].to_dict(), "depends_on_slot_ids": ["b"]}
                ),
                template.node_specs[1].__class__(
                    **{**template.node_specs[1].to_dict(), "slot_id": "b", "depends_on_slot_ids": [template.node_specs[0].slot_id]}
                ),
            ),
            edge_specs=(EdgeSpec(template.node_specs[0].slot_id, "b", "precedes", "测试"),),
        )


def test_node_draft_requires_evidence_for_each_claim_and_round_trips():
    draft = _draft()
    assert NodeDraft.from_dict(draft.to_dict()).to_dict() == draft.to_dict()

    with pytest.raises(TraceContractError, match="cannot be empty"):
        NodeClaim("claim-bad", "action", "没有依据的主张", evidence_ids=[])

    claim_payload = _draft().claims[0].to_dict()
    claim_payload["description_note"] = "not part of the claim contract"
    with pytest.raises(TraceContractError, match="unsupported fields: description_note"):
        NodeClaim.from_dict(claim_payload)

    bad = draft.to_dict()
    bad["claims"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(TraceContractError, match="unknown evidence"):
        NodeDraft.from_dict(bad)


def test_conclusion_ir_round_trips_headings_lists_and_tables():
    ir = ConclusionIR(
        schema_version="conclusion-ir.v1",
        blocks=[
            {"type": "heading", "level": 2, "text": "响应与处置", "source_fragment": "二、响应与处置"},
            {"type": "paragraph", "text": "多方材料显示处置已经启动。", "source_fragment": "多方材料显示处置已经启动。"},
            {"type": "list", "ordered": True, "items": ["机构甲发布说明", "机构乙完成复核"], "source_fragment": "①机构甲发布说明；②机构乙完成复核。"},
            {"type": "table", "title": "主体—行动—结果—状态", "columns": ["主体", "行动", "结果", "状态"], "rows": [["机构甲", "发布说明", "信息公开", "已确认"]], "source_fragment": "主体—行动—结果—状态：机构甲—发布说明—信息公开—已确认。"},
        ],
        source_hash="abc",
    )
    assert ConclusionIR.from_dict(ir.to_dict()).to_dict() == ir.to_dict()
    assert all("source_fragment" not in block for block in ir.to_dict()["blocks"])


def test_conclusion_ir_discards_legacy_source_fragments():
    legacy = {
        "schema_version": "conclusion-ir.v1",
        "blocks": [{"type": "paragraph", "text": "已整理的结论。", "source_fragment": "旧版逐字来源片段。"}],
    }

    assert ConclusionIR.from_dict(legacy).to_dict()["blocks"] == [
        {"type": "paragraph", "text": "已整理的结论。"},
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": "conclusion-ir.v1", "blocks": [{"type": "unknown", "text": "x"}]},
        {"schema_version": "conclusion-ir.v1", "blocks": [{"type": "heading", "level": 4, "text": "x"}]},
        {"schema_version": "conclusion-ir.v1", "blocks": [{"type": "table", "columns": ["主体", "行动"], "rows": [["只有一列"]]}]},
    ],
)
def test_conclusion_ir_rejects_unsupported_or_malformed_blocks(bad):
    with pytest.raises(TraceContractError):
        ConclusionIR.from_dict(bad)


def test_claim_sources_must_be_backed_by_its_linked_evidence():
    with pytest.raises(TraceContractError, match="not covered by its evidence"):
        NodeDraft(
            node_id="node-1", slot_id="response_treatment", revision=0,
            claims=[NodeClaim("c1", "action", "机构采取了处置行动。", source_document_ids=["doc-2"], evidence_ids=["e1"])],
            evidence=[NodeEvidence("e1", "c1", "doc-1", "原文片段", {"url": "https://example.test/source"})],
            conclusion="机构采取了处置行动。", media=[],
        )


def test_one_claim_can_synthesize_multiple_source_documents():
    draft = NodeDraft(
        node_id="node-1", slot_id="response_treatment", revision=0,
        claims=[NodeClaim("c1", "action", "不同来源共同显示机构已启动处置。", source_document_ids=["doc-1", "doc-2"], evidence_ids=["e1", "e2"])],
        evidence=[
            NodeEvidence("e1", "c1", "doc-1", "机构启动抽检。", {"url": "https://example.test/one"}),
            NodeEvidence("e2", "c1", "doc-2", "主管部门公布抽检安排。", {"url": "https://example.test/two"}),
        ],
        conclusion="多份来源显示处置已经启动。", media=[],
    )
    assert draft.claims[0].source_document_ids == ["doc-1", "doc-2"]


def test_media_requires_traceable_source_and_review_rounds_are_fixed():
    media = NodeMedia(
        media_id="media-1",
        media_type="image",
        document_id="doc-1",
        caption="现场照片",
        why_relevant="帮助识别材料中的现场状态。",
        source_url="https://example.test/image.jpg",
    )
    assert NodeMedia.from_dict(media.to_dict()).to_dict() == media.to_dict()
    with pytest.raises(TraceContractError, match="exactly one"):
        NodeMedia("bad", "image", "doc-1", "图片", "用途", source_url="https://a", object_key="obj")

    review = NodeReview(
        review_id="review-1",
        node_id="node-1",
        slot_id="response_treatment",
        revision=1,
        status=NodeReviewStatus.REVISE,
        covered_aspects=["主体"],
        missing_aspects=["结果"],
        redundant_claims=[],
        unclear_claims=[],
        evidence_followups=["补充结果来源"],
        rewrite_instructions=["分别描述不同主体"],
        next_research_tasks=["检索处置结果"],
        final_editor_note="第一轮补充建议。",
    )
    assert NodeReview.from_dict(review.to_dict()).to_dict() == review.to_dict()
    with pytest.raises(TraceContractError, match="must be finalize"):
        NodeReview("bad", "node-1", "response_treatment", 3, "revise", [], [], [], [], [], [], [], "说明")


def test_frozen_node_requires_initial_draft_plus_three_reviewed_revisions():
    revisions = [NodeRevision("node-1", "response_treatment", 0, _draft(0))]
    for revision in range(1, 4):
        status = "finalize" if revision == 3 else "revise"
        review = NodeReview(
            review_id=f"review-{revision}",
            node_id="node-1",
            slot_id="response_treatment",
            revision=revision,
            status=status,
            covered_aspects=["主体"],
            missing_aspects=[] if revision == 3 else ["结果"],
            redundant_claims=[],
            unclear_claims=[],
            evidence_followups=[] if revision == 3 else ["补充"],
            rewrite_instructions=["编辑"],
            next_research_tasks=[] if revision == 3 else ["检索"],
            final_editor_note="定稿" if revision == 3 else "继续优化。",
        )
        revisions.append(NodeRevision("node-1", "response_treatment", revision, _draft(revision), review, revision - 1))

    frozen = FrozenTraceNode("node-1", "response_treatment", _draft(3), revisions)
    restored = FrozenTraceNode.from_dict(frozen.to_dict())
    assert restored.revisions[-1].review.status == NodeReviewStatus.FINALIZE

    with pytest.raises(TraceContractError, match="exactly three revisions"):
        FrozenTraceNode("node-1", "response_treatment", _draft(3), revisions[:3])
