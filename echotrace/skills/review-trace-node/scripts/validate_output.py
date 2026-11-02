#!/usr/bin/env python3
"""Validate the machine-readable contract for a node review."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echotrace.domain.trace_nodes import NodeReview  # noqa: E402

REQUIRED = {
    "review_id", "node_id", "slot_id", "revision", "status", "covered_aspects",
    "missing_aspects", "redundant_claims", "unclear_claims", "evidence_followups",
    "rewrite_instructions", "next_research_tasks", "final_editor_note",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        value = json.loads(args.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID_OUTPUT: {exc}")
        return 1
    if not isinstance(value, dict):
        print("INVALID_OUTPUT: top level must be an object")
        return 1
    missing = sorted(REQUIRED - set(value))
    if missing:
        print("INVALID_OUTPUT: missing " + ", ".join(missing))
        return 1
    unknown = sorted(set(value) - REQUIRED)
    if unknown:
        print("INVALID_OUTPUT: unknown top-level keys " + ", ".join(unknown))
        return 1
    list_fields = sorted(REQUIRED - {"review_id", "node_id", "slot_id", "revision", "status", "final_editor_note"})
    for field in list_fields:
        if not isinstance(value[field], list):
            print(f"INVALID_OUTPUT: {field} must be an array")
            return 1
        if any(not isinstance(item, str) or not item.strip() for item in value[field]):
            print(f"INVALID_OUTPUT: {field} must contain non-empty strings")
            return 1
    try:
        review = NodeReview.from_dict(value)
    except Exception as exc:  # domain contract errors are user-facing validator output
        print(f"INVALID_OUTPUT: {exc}")
        return 1
    if review.revision == 3 and review.status.value != "finalize":
        print("INVALID_OUTPUT: revision 3 must finalize the node")
        return 1
    print("OK: node review output contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
