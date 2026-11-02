#!/usr/bin/env python3
"""Validate the machine-readable contract for a node research draft."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from echotrace.domain.trace_nodes import NodeDraft  # noqa: E402

REQUIRED = {"node_id", "slot_id", "revision", "claims", "evidence", "conclusion", "media"}
OPTIONAL = {"metrics", "datasets"}


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
    unknown = sorted(set(value) - REQUIRED - OPTIONAL)
    if unknown:
        print("INVALID_OUTPUT: unknown top-level keys " + ", ".join(unknown))
        return 1
    for field in ("claims", "evidence", "media"):
        if not isinstance(value[field], list):
            print(f"INVALID_OUTPUT: {field} must be an array")
            return 1
    for index, item in enumerate(value["evidence"]):
        if not isinstance(item, dict):
            print(f"INVALID_OUTPUT: evidence[{index}] must be an object")
            return 1
        if not isinstance(item.get("locator"), dict) or not item["locator"]:
            print(f"INVALID_OUTPUT: evidence[{index}] locator must be a non-empty object")
            return 1
    try:
        draft = NodeDraft.from_dict(value)
    except Exception as exc:  # domain contract errors are user-facing validator output
        print(f"INVALID_OUTPUT: {exc}")
        return 1
    claim_ids = {claim.claim_id for claim in draft.claims}
    evidence_ids = {evidence.evidence_id for evidence in draft.evidence}
    for evidence in draft.evidence:
        if evidence.relation.value not in {"supports", "contradicts", "mentions"}:
            print(f"INVALID_OUTPUT: unsupported evidence relation {evidence.relation}")
            return 1
        if evidence.claim_id not in claim_ids:
            print(f"INVALID_OUTPUT: evidence[{evidence.evidence_id}] references unknown claim")
            return 1
    for claim in draft.claims:
        if not set(claim.evidence_ids).issubset(evidence_ids):
            print(f"INVALID_OUTPUT: claim[{claim.claim_id}] references unknown evidence")
            return 1
        if not claim.source_document_ids:
            print(f"INVALID_OUTPUT: claim[{claim.claim_id}] must identify source_document_ids")
            return 1
        claim_evidence_documents = {
            evidence.document_id
            for evidence in draft.evidence
            if evidence.evidence_id in claim.evidence_ids
        }
        if not set(claim.source_document_ids).issubset(claim_evidence_documents):
            print(f"INVALID_OUTPUT: claim[{claim.claim_id}] source_document_ids must be covered by its evidence")
            return 1
        if any(claim.text.strip() == evidence.excerpt.strip() for evidence in draft.evidence if evidence.evidence_id in claim.evidence_ids):
            print(f"INVALID_OUTPUT: claim[{claim.claim_id}] must synthesize evidence instead of copying an excerpt")
            return 1
    print("OK: node research output contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
