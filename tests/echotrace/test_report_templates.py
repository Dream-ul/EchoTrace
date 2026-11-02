from pathlib import Path

import pytest

from echotrace.application.report_templates import load_report_template
from echotrace.domain import TraceContractError, get_trace_template


@pytest.fixture
def investigation_template_fixture():
    return {
        "public_event": (
            "origin_early_signals", "event_scope", "spread_amplification",
            "turning_point", "response_treatment", "current_outcome",
        ),
        "public_opinion": (
            "trigger_content", "initial_spread", "core_positions",
            "narrative_shift", "response_repair", "current_opinion_state",
        ),
        "product_safety": (
            "product_scope", "first_anomaly", "affected_scope",
            "investigation_findings", "remediation_recall", "retest_closure",
        ),
        "consumer_dispute": (
            "transaction_service", "first_feedback", "merchant_platform_response",
            "escalation_intervention", "remedy_fulfillment", "unresolved_items",
        ),
        "official_claim": (
            "original_statement", "claim_breakdown", "evidence_comparison",
            "clarification_revision", "current_assessment",
        ),
        "policy_regulation": (
            "policy_signal", "formal_release", "effective_scope",
            "implementation", "impact_feedback", "revision_withdrawal",
        ),
    }


@pytest.fixture
def report_template_fixture(investigation_template_fixture):
    return {
        key: load_report_template(key)
        for key in investigation_template_fixture
    }


def test_six_report_templates_match_node_templates_and_cover_all_slots(
    investigation_template_fixture,
    report_template_fixture,
):
    assert set(report_template_fixture) == set(investigation_template_fixture)
    for investigation_type, report in report_template_fixture.items():
        trace = get_trace_template(investigation_type)
        expected_slots = investigation_template_fixture[investigation_type]
        assert tuple(node.slot_id for node in trace.node_specs) == expected_slots
        assert report.template_id == f"{investigation_type}.report"
        assert report.investigation_type == trace.investigation_type
        assert report.trace_template_id == trace.template_id
        assert report.trace_template_version == trace.template_version
        assert report.language == "zh-CN"
        assert len(report.chapters) == 5
        assert {slot for chapter in report.chapters for slot in chapter.node_slots} == set(expected_slots)
        assert [chapter.title for chapter in report.chapters] == [
            line.removeprefix("## ")
            for line in report.instructions.splitlines()
            if line.startswith("## ")
        ]
        assert report.content_hash
        assert report.datasets
        assert report.visuals
        assert all(len(chapter.subheadings) >= 2 for chapter in report.chapters)
        assert all(heading.level in {3, 4} for chapter in report.chapters for heading in chapter.subheadings)
        dataset_ids = {dataset.dataset_id for dataset in report.datasets}
        assert all(visual.dataset_id in dataset_ids for visual in report.visuals)


def test_public_opinion_template_declares_discourse_analysis_dimensions(report_template_fixture):
    report = report_template_fixture["public_opinion"]
    needs = {need for chapter in report.chapters for need in chapter.data_needs}
    assert {"sentiment", "stance", "topic"}.issubset(needs)
    assert "样本" in report.instructions
    assert {dataset.dataset_id for dataset in report.datasets} >= {
        "discussion_trend", "sentiment_distribution", "topic_positions",
    }
    assert {visual.type for visual in report.visuals} >= {"line", "pie"}


def test_report_template_rejects_trace_version_mismatch(tmp_path, report_template_fixture):
    source_path = Path(__file__).parents[2] / "echotrace" / "report_templates" / "public_event.md"
    source = source_path.read_text(encoding="utf-8")
    source = source.replace(
        "trace_template_version: trace-template.v1",
        "trace_template_version: trace-template.v0",
        1,
    )
    (tmp_path / "public_event.md").write_text(source, encoding="utf-8")

    with pytest.raises(TraceContractError, match="trace_template_version"):
        load_report_template("public_event", template_root=tmp_path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("node_slots: [origin_early_signals, event_scope]", "node_slots: [unknown_slot]", "unknown node slots"),
        ("recommended_blocks: [paragraph, metric_grid]", "recommended_blocks: [made_up]", "unsupported blocks"),
        ("## 执行摘要", "## 摘要", "headings do not match"),
    ],
)
def test_report_template_rejects_invalid_mappings_and_structure(tmp_path, old, new, message):
    source_path = Path(__file__).parents[2] / "echotrace" / "report_templates" / "public_event.md"
    source = source_path.read_text(encoding="utf-8")
    assert old in source
    (tmp_path / "public_event.md").write_text(source.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(TraceContractError, match=message):
        load_report_template("public_event", template_root=tmp_path)
