from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[2] / "echotrace" / "skills"


def run_validator(skill: str, payload: dict) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "output.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(ROOT / skill / "scripts" / "validate_output.py"), str(path)],
            capture_output=True,
            text=True,
            check=False,
        )


def draft(revision: int = 0) -> dict:
    return {
        "node_id": "node-1",
        "slot_id": "origin_early_signals",
        "revision": revision,
        "claims": [{
            "claim_id": "claim-1",
            "claim_type": "observation",
            "text": "主体在指定时间发布了早期信号。",
            "subject": "主体",
            "predicate": "发布",
            "object_value": "早期信号",
            "time_scope": "2026-01-01",
            "source_document_ids": ["doc-1"],
            "evidence_ids": ["evidence-1"],
        }],
        "evidence": [{
            "evidence_id": "evidence-1",
            "claim_id": "claim-1",
            "document_id": "doc-1",
            "excerpt": "主体于2026年1月1日发布公告。",
            "locator": {"url": "https://example.test/a", "paragraph": 2},
            "relation": "supports",
        }],
        "conclusion": "目前可确认主体在该日期发布了早期信号。",
        "media": [],
    }


def review(revision: int, status: str, *, open_items: bool = True) -> dict:
    return {
        "review_id": f"review-{revision}",
        "node_id": "node-1",
        "slot_id": "origin_early_signals",
        "revision": revision,
        "status": status,
        "covered_aspects": ["时间", "主体"],
        "missing_aspects": ["早期不确定性"] if open_items else [],
        "redundant_claims": [],
        "unclear_claims": ["claim-1 的范围需要更清楚"] if open_items else [],
        "evidence_followups": ["补充首发材料定位"] if open_items else [],
        "rewrite_instructions": ["明确发布主体和来源时间"] if open_items else [],
        "next_research_tasks": ["检索原始公告"] if open_items else [],
        "final_editor_note": "本轮编辑意见。",
    }


def test_research_validator_accepts_claim_bound_evidence():
    result = run_validator("research-trace-node", draft())
    assert result.returncode == 0, result.stdout


def test_research_validator_accepts_exact_media_contract():
    value = draft()
    value["media"] = [{
        "media_id": "media-1", "media_type": "image", "document_id": "doc-1",
        "caption": "原始页面中的配图", "why_relevant": "展示相关处置页面",
        "source_url": "https://example.test/image.jpg",
    }]

    result = run_validator("research-trace-node", value)
    assert result.returncode == 0, result.stdout


def test_research_validator_rejects_media_type_alias():
    value = draft()
    value["media"] = [{
        "media_id": "media-1", "type": "image", "document_id": "doc-1",
        "caption": "原始页面中的配图", "why_relevant": "展示相关处置页面",
        "source_url": "https://example.test/image.jpg",
    }]

    result = run_validator("research-trace-node", value)
    assert result.returncode == 1
    assert "use media_type" in result.stdout


def test_research_validator_accepts_evidence_grounded_metrics_and_datasets():
    value = draft()
    value["metrics"] = [{
        "metric_id": "metric-1",
        "name": "公告提及数量",
        "value": 2,
        "unit": "篇",
        "time_scope": "2026-01-01",
        "source_document_id": "doc-1",
        "evidence_id": "evidence-1",
        "locator": {"url": "https://example.test/a", "paragraph": 2},
    }]
    value["datasets"] = [{
        "dataset_id": "dataset-1",
        "purpose": "呈现公告数量",
        "columns": ["日期", "数量"],
        "rows": [{"日期": "2026-01-01", "数量": 2}],
        "grain": "按日",
        "sample_scope": "已提供来源",
        "time_scope": "2026-01-01",
        "method": "直接汇总已核实来源中的明确数量",
        "input_metric_ids": ["metric-1"],
    }]

    result = run_validator("research-trace-node", value)
    assert result.returncode == 0, result.stdout


def test_research_validator_rejects_unbound_claim():
    value = draft()
    value["claims"][0]["evidence_ids"] = []
    result = run_validator("research-trace-node", value)
    assert result.returncode == 1
    assert "evidence_ids" in result.stdout or "cannot be empty" in result.stdout


def test_research_validator_rejects_empty_locator():
    value = draft()
    value["evidence"][0]["locator"] = {}
    result = run_validator("research-trace-node", value)
    assert result.returncode == 1
    assert "locator" in result.stdout


def test_research_validator_requires_source_mapping_and_synthesized_claim_text():
    value = draft()
    value["claims"][0]["source_document_ids"] = []
    result = run_validator("research-trace-node", value)
    assert result.returncode == 1
    assert "source_document_ids" in result.stdout

    value = draft()
    value["claims"][0]["text"] = value["evidence"][0]["excerpt"]
    result = run_validator("research-trace-node", value)
    assert result.returncode == 1
    assert "synthesize" in result.stdout


def test_review_validator_enforces_revise_rounds():
    result = run_validator("review-trace-node", review(1, "revise"))
    assert result.returncode == 0, result.stdout
    result = run_validator("review-trace-node", review(2, "finalize"))
    assert result.returncode == 1
    assert "must be revise" in result.stdout


def test_review_validator_requires_closed_final_round():
    result = run_validator("review-trace-node", review(3, "finalize"))
    assert result.returncode == 1
    assert "final review cannot retain" in result.stdout
    result = run_validator("review-trace-node", review(3, "finalize", open_items=False))
    assert result.returncode == 0, result.stdout

    value = review(3, "finalize", open_items=False)
    del value["missing_aspects"]
    result = run_validator("review-trace-node", value)
    assert result.returncode == 1
    assert "missing_aspects" in result.stdout


def test_review_validator_rejects_global_gate_shape():
    result = run_validator("review-trace-node", {"overall_gate": "PASS"})
    assert result.returncode == 1
    assert "missing" in result.stdout
