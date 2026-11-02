"""Observable progress state for one node Researcher session.

Search and tool counters are diagnostics only.  They never decide that a
node is complete and never revoke retrieval.  The Researcher declares a
semantic checkpoint when the fixed aspects and review tasks are covered;
runtime deadlines and provider limits remain failure safeguards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping


SEARCH_CAPABILITIES = frozenset({
    "search_web", "fetch_public_page", "ingest_public_feed", "normalize_documents",
})

_EVIDENCE_GAP_MARKERS = (
    "draft.claims cannot be empty",
    "draft.evidence cannot be empty",
    "evidence references unavailable source documents",
    "evidence references unavailable source document",
    # The live runner reports an unknown source while validating the nested
    # NodeDraft payload as ``evidence.document_id=<id>``.  This is still an
    # evidence gap: the researcher must be allowed to retrieve or verify the
    # source before the next delivery attempt.
    "evidence.document_id=",
    "证据 document_id",
    "unknown source document",
    "source document is unavailable",
)


def contract_error_requires_research(error: Any) -> bool:
    """Only reopen retrieval when validation identifies a missing source fact.

    Schema and link-shape errors can usually be repaired from the evidence
    already in the task context. Treating every invalid payload as an evidence
    gap causes needless searches and can trap finalization in a retrieval loop.
    """

    message = str(error or "").lower()
    return any(marker in message for marker in _EVIDENCE_GAP_MARKERS)


def _text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _documents(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    records: list[Mapping[str, Any]] = []
    for key in ("documents", "results", "records", "items"):
        values = value.get(key)
        if isinstance(values, list):
            records.extend(item for item in values if isinstance(item, Mapping))
    document = value.get("document")
    if isinstance(document, Mapping):
        records.append(document)
    return records


def _document_id(value: Mapping[str, Any]) -> str:
    return str(value.get("document_id") or value.get("id") or value.get("url") or "").strip()


@dataclass(slots=True)
class ResearchProgress:
    """Small mutable state shared by the gateway, prompt pack and runtime."""

    required_aspects: tuple[str, ...] = ()
    targeted_tasks: tuple[str, ...] = ()
    revision: int = 0
    search_calls: int = 0
    tool_calls: int = 0
    novel_documents: int = 0
    consecutive_no_progress: int = 0
    seen_queries: set[str] = field(default_factory=set)
    planned_queries: set[str] = field(default_factory=set)
    seen_document_ids: set[str] = field(default_factory=set)
    covered_aspects: set[str] = field(default_factory=set)
    completed_tasks: set[str] = field(default_factory=set)
    stop_reason: str | None = None
    finalize_only: bool = False
    force_finalize: bool = False
    finalize_only_nonfinal_turns: int = 0

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> "ResearchProgress":
        task = context.get("task") if isinstance(context.get("task"), Mapping) else {}
        state = context.get("state") if isinstance(context.get("state"), Mapping) else {}
        spec = context.get("node_spec") if isinstance(context.get("node_spec"), Mapping) else task.get("node_spec")
        spec = spec if isinstance(spec, Mapping) else {}
        review = context.get("review_feedback") if isinstance(context.get("review_feedback"), Mapping) else state.get("review_feedback")
        review = review if isinstance(review, Mapping) else {}
        raw = context.get("research_progress") if isinstance(context.get("research_progress"), Mapping) else {}
        required = tuple(str(item).strip() for item in spec.get("aspects", ()) if str(item).strip())
        targeted = tuple(
            str(item).strip()
            for key in ("missing_aspects", "evidence_followups", "next_research_tasks")
            for item in (review.get(key) or ())
            if str(item).strip()
        )
        progress = cls(
            required_aspects=required,
            targeted_tasks=targeted,
            revision=int(context.get("revision") or task.get("revision") or 0),
            search_calls=max(0, int(raw.get("search_calls", 0) or 0)),
            tool_calls=max(0, int(raw.get("tool_calls", 0) or 0)),
            novel_documents=max(0, int(raw.get("novel_documents", 0) or 0)),
            consecutive_no_progress=max(0, int(raw.get("consecutive_no_progress", 0) or 0)),
            seen_queries={_text(item) for item in raw.get("seen_queries", ()) if _text(item)},
            planned_queries={_text(item) for item in raw.get("planned_queries", ()) if _text(item)},
            seen_document_ids={str(item).strip() for item in raw.get("seen_document_ids", ()) if str(item).strip()},
            covered_aspects={_text(item) for item in raw.get("covered_aspects", ()) if _text(item)},
            completed_tasks={_text(item) for item in raw.get("completed_tasks", ()) if _text(item)},
            stop_reason=str(raw.get("stop_reason") or "").strip() or None,
            finalize_only=bool(raw.get("finalize_only", False)),
            force_finalize=bool(raw.get("force_finalize", False)),
            finalize_only_nonfinal_turns=max(0, int(raw.get("finalize_only_nonfinal_turns", 0) or 0)),
        )
        # Existing durable source records are evidence already available to a
        # resumed task.  They must not be counted as newly discovered results
        # when a worker retry repeats a query.
        evidence_resources = context.get("evidence") if isinstance(context.get("evidence"), list) else []
        for item in evidence_resources:
            if not isinstance(item, Mapping):
                continue
            card = item.get("document_card") if isinstance(item.get("document_card"), Mapping) else {}
            identifier = card.get("document_id") or item.get("document_id")
            if not identifier:
                resource_id = str(item.get("resource_id") or "")
                if resource_id.startswith("document:"):
                    identifier = resource_id.split(":", 1)[1]
            if str(identifier or "").strip():
                progress.seen_document_ids.add(str(identifier).strip())
        # Revision 3 is normally an editorial pass, but a concrete evidence
        # gap in the third review may require one more targeted source lookup.
        # The Researcher decides whether that lookup is needed; the runtime
        # does not revoke tools solely because the revision number is 3.
        return progress

    @property
    def phase(self) -> str:
        if self.finalize_only:
            return "synthesis"
        return "targeted" if self.revision else "explore"

    def observe_tool(self, capability: str, arguments: Mapping[str, Any], result: Any) -> None:
        self.tool_calls += 1
        if capability not in SEARCH_CAPABILITIES:
            self._check_limits()
            return
        self.search_calls += 1
        query = _text(arguments.get("query") if isinstance(arguments, Mapping) else "")
        if query:
            self.seen_queries.add(query)
        before = len(self.seen_document_ids)
        for item in _documents(result):
            identity = _document_id(item)
            if identity:
                self.seen_document_ids.add(identity)
        added = len(self.seen_document_ids) - before
        self.novel_documents += added
        self.consecutive_no_progress = 0 if added else self.consecutive_no_progress + 1
        self._check_limits()

    def query_was_seen(self, query: Any) -> bool:
        """Return whether a normalized retrieval query was already attempted."""

        normalized = _text(query)
        return bool(normalized and normalized in self.seen_queries)

    def admit_query(self, query: Any) -> bool:
        """Reserve a novel query for one tool call.

        ``planned_queries`` comes from Verifier feedback and is guidance for
        the Researcher, not an allow-list.  A researcher often needs to
        refine a suggested query after seeing search results (for example by
        adding an institution, date, or document type).  Rejecting every
        query that is not an exact feedback string traps the agent in a loop
        where it keeps requesting a search but never reaches the provider.
        Exact repeats remain rejected to avoid wasting provider calls.
        """

        normalized = _text(query)
        if not normalized:
            return True
        if normalized in self.seen_queries:
            return False
        # Planned queries are advisory.  Remove an exact planned query when
        # it is used, while allowing the model to issue a new targeted query
        # derived from the same review task.
        self.planned_queries.discard(normalized)
        self.seen_queries.add(normalized)
        return True

    def accept_checkpoint(self, checkpoint: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Validate a model progress checkpoint without judging prose quality.

        The runtime can verify that the model named every fixed NodeSpec aspect
        before it voluntarily stops retrieval. It cannot decide whether a
        claim is substantively good; that remains the Verifier's three-round
        editorial responsibility. Forced stops caused by a failure budget are
        explicitly accepted and carried forward as non-quality outcomes.
        """

        status = _text(checkpoint.get("status"))
        covered = {
            _text(item)
            for item in (checkpoint.get("covered_aspects") or ())
            if _text(item)
        }
        self.covered_aspects.update(covered)
        completed = {
            _text(item)
            for item in (checkpoint.get("completed_tasks") or ())
            if _text(item)
        }
        self.completed_tasks.update(completed)
        if status == "continue":
            if self.finalize_only:
                return False, {
                    "status": "continue",
                    "error_code": "research_finalize_only",
                    "stop_reason": self.stop_reason,
                    "required_action": "submit the best evidence-grounded NodeDraft without further retrieval",
                }
            queries = [
                _text(item)
                for item in (checkpoint.get("next_queries") or ())
                if _text(item)
            ]
            repeated = [item for item in queries if item in self.seen_queries or item in self.planned_queries]
            if repeated:
                return False, {
                    "status": "continue",
                    "error_code": "repeated_research_query",
                    "repeated_queries": repeated[:8],
                    "required_action": "provide new targeted queries or declare ready",
                }
            if not queries:
                return False, {
                    "status": "continue",
                    "error_code": "missing_next_queries",
                    "required_action": "list concrete new next_queries",
                }
            self.planned_queries.update(queries)
            return True, {
                "status": "continue",
                "next_queries": queries[:8],
                "covered_aspects": sorted(covered)[:32],
                "completed_tasks": sorted(completed)[:32],
            }
        if status != "ready":
            return False, {
                "status": status,
                "error_code": "invalid_checkpoint_status",
                "required_action": "use continue or ready",
            }

        forced = self.force_finalize

        missing = [
            aspect for aspect in self.required_aspects
            if _text(aspect) not in self.covered_aspects
        ]
        if missing and not self.force_finalize:
            return False, {
                "status": "ready",
                "error_code": "uncovered_required_aspects",
                "missing_aspects": missing[:16],
                "required_action": "continue targeted research for every missing aspect",
            }
        missing_tasks = [
            task for task in self.targeted_tasks
            if _text(task) not in self.completed_tasks
        ]
        if missing_tasks and not self.force_finalize:
            return False, {
                "status": "ready",
                "error_code": "incomplete_review_tasks",
                "missing_tasks": missing_tasks[:16],
                "required_action": "complete every concrete review task before ready",
            }
        if not self.finalize_only:
            self.request_finalize("model_declared_ready", forced=False)
        return True, {
            "status": "ready",
            "covered_aspects": sorted(self.covered_aspects)[:32],
            "missing_aspects": missing[:16],
            "completed_tasks": sorted(self.completed_tasks)[:32],
            "missing_tasks": missing_tasks[:16],
            "forced": forced,
            "stop_reason": self.stop_reason,
        }

    def request_finalize(self, reason: str, *, forced: bool = True) -> None:
        self.finalize_only = True
        self.force_finalize = self.force_finalize or forced
        if not self.stop_reason:
            self.stop_reason = reason

    def reopen_for_research(self, reason: str = "contract_repair_requires_evidence") -> None:
        """Re-enable targeted retrieval when delivery validation finds a gap."""

        self.finalize_only = False
        self.force_finalize = False
        self.stop_reason = reason

    def _check_limits(self) -> None:
        """Keep counters observable without using them as quality stops."""
        return

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "revision": self.revision,
            "required_aspects": list(self.required_aspects),
            "targeted_tasks": list(self.targeted_tasks[:16]),
            "search_calls": self.search_calls,
            "tool_calls": self.tool_calls,
            "novel_documents": self.novel_documents,
            "consecutive_no_progress": self.consecutive_no_progress,
            "seen_query_count": len(self.seen_queries),
            "planned_queries": sorted(self.planned_queries)[:16],
            "source_count": len(self.seen_document_ids),
            "covered_aspects": sorted(self.covered_aspects)[:32],
            "completed_tasks": sorted(self.completed_tasks)[:32],
            "missing_aspects": [
                aspect for aspect in self.required_aspects
                if _text(aspect) not in self.covered_aspects
            ][:16],
            "missing_tasks": [
                task for task in self.targeted_tasks
                if _text(task) not in self.completed_tasks
            ][:16],
            "stop_reason": self.stop_reason,
            "finalize_only": self.finalize_only,
            "force_finalize": self.force_finalize,
            "finalize_only_nonfinal_turns": self.finalize_only_nonfinal_turns,
            "stop_rule": "研究者确认固定方面和具体审稿任务都有证据支持后主动 ready；计数仅作诊断。",
        }


def default_research_policy(revision: int) -> dict[str, Any]:
    """Return research guidance; counts are advisory, not stop conditions."""

    return {
        "phase": "targeted" if revision > 0 else "explore",
        "search_allowed": True,
        "stop_rule": "由研究者确认固定方面和审稿任务都有证据支持后交付；计数仅作诊断",
    }


__all__ = [
    "ResearchProgress",
    "SEARCH_CAPABILITIES",
    "contract_error_requires_research",
    "default_research_policy",
]
