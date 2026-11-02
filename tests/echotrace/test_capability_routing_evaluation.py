from pathlib import Path

from echotrace.evaluation import load_capability_routing_cases, score_capability_routing


CASES = Path(__file__).parents[2] / "echotrace" / "evaluation" / "capability_routing_cases.json"


def test_capability_routing_fixture_is_scored_without_a_model_call():
    cases = load_capability_routing_cases(CASES)
    predictions = {
        "discover-public-reporting": {
            "decision": "select",
            "capability": "search_web",
            "payload": {"query": "product recall", "limit": 5},
        },
        "private-source-boundary": {"decision": "select", "capability": "search_web", "payload": {"query": "private posts"}},
        "sentiment-as-fact-boundary": {"decision": "reject"},
    }

    score = score_capability_routing(cases, predictions)

    assert score["case_count"] == 10
    assert score["capability_top1_accuracy"] == 0.125
    assert score["payload_schema_pass_rate"] == 1.0
    assert score["policy_violation_rate"] == 0.5


def test_routing_evaluation_counts_unknown_capabilities_and_invalid_payloads():
    cases = load_capability_routing_cases(CASES)
    score = score_capability_routing(
        cases,
        {
            "discover-public-reporting": {"decision": "select", "capability": "web_search", "payload": {"query": "x"}},
            "ingest-known-feed": {"decision": "select", "capability": "ingest_public_feed", "payload": {}},
        },
    )

    assert score["invalid_capability_rate"] == 0.5
    assert score["payload_schema_pass_rate"] == 0.0
