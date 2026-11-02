"""Deterministic scoring for planner capability-routing predictions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from echotrace.runtime.tool_gateway.contracts import CAPABILITY_CATALOG, ToolContractError, validate_capability_input


def load_capability_routing_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load a checked-in routing set without invoking a model or provider."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("capability routing cases must be a JSON list")
    return [dict(item) for item in payload]


def score_capability_routing(
    cases: Iterable[Mapping[str, Any]],
    predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Score structured planner decisions against fixed expected behavior.

    Each prediction has ``decision`` (``select`` or ``reject``), optional
    ``capability``, and optional ``payload``.  The function intentionally does
    not judge natural-language rationale; that belongs in a separate review
    rubric once an LLM Planner exists.
    """
    rows: list[dict[str, Any]] = []
    selection_cases = 0
    correct_selections = 0
    emitted_capabilities = 0
    invalid_capabilities = 0
    selected_formal_capabilities = 0
    valid_payloads = 0
    rejected_cases = 0
    policy_violations = 0
    agent_policy_violations = 0

    for case in cases:
        case_id = str(case["id"])
        expected = dict(case["expected"])
        prediction = dict(predictions.get(case_id, {}))
        expected_decision = expected.get("decision")
        actual_decision = prediction.get("decision")
        capability = prediction.get("capability")
        payload = prediction.get("payload") or {}
        is_formal_capability = isinstance(capability, str) and capability in CAPABILITY_CATALOG
        payload_valid = False

        if capability is not None:
            emitted_capabilities += 1
            if not is_formal_capability:
                invalid_capabilities += 1
        if actual_decision == "select" and is_formal_capability:
            selected_formal_capabilities += 1
            spec = CAPABILITY_CATALOG[capability]
            agent_policy_violations += int(bool(spec.agents) and case.get("agent") not in spec.agents)
            try:
                validate_capability_input(capability, dict(payload))
                payload_valid = True
                valid_payloads += 1
            except (ToolContractError, TypeError):
                pass

        capability_correct = (
            expected_decision == "select"
            and actual_decision == "select"
            and capability == expected.get("capability")
        )
        if expected_decision == "select":
            selection_cases += 1
            correct_selections += int(capability_correct)
        if expected_decision == "reject":
            rejected_cases += 1
            policy_violations += int(actual_decision == "select")

        rows.append(
            {
                "id": case_id,
                "decision_correct": actual_decision == expected_decision,
                "capability_correct": capability_correct,
                "payload_valid": payload_valid,
                "policy_violation": expected_decision == "reject" and actual_decision == "select",
            }
        )

    total = len(rows)
    return {
        "case_count": total,
        "decision_accuracy": _ratio(sum(row["decision_correct"] for row in rows), total),
        "capability_top1_accuracy": _ratio(correct_selections, selection_cases),
        "invalid_capability_rate": _ratio(invalid_capabilities, emitted_capabilities),
        "payload_schema_pass_rate": _ratio(valid_payloads, selected_formal_capabilities),
        "policy_violation_rate": _ratio(policy_violations, rejected_cases),
        "agent_policy_violation_rate": _ratio(agent_policy_violations, selected_formal_capabilities),
        "cases": rows,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None
