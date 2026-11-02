"""Schema utilities used by migration smoke tests and local development."""

from __future__ import annotations

from sqlalchemy import inspect

from .database import Database


REQUIRED_TABLES = {
    "source_documents", "source_clusters", "source_cluster_members", "analysis_features",
    "trace_templates", "trace_template_nodes", "trace_template_edges", "investigation_nodes",
    "node_revisions", "node_claims", "node_evidence", "node_media", "node_reviews",
    "events", "event_documents", "investigation_runs", "task_plans", "investigation_tasks",
    "task_dependencies", "task_attempts", "run_checkpoints", "run_events",
    "agent_skill_definitions", "agent_skill_activations", "tool_provider_definitions",
    "tool_calls", "outbox_events",
}


def assert_schema_complete(database: Database) -> None:
    actual = set(inspect(database.engine).get_table_names())
    missing = REQUIRED_TABLES - actual
    if missing:
        raise RuntimeError(f"database schema is missing required tables: {sorted(missing)}")


__all__ = ["REQUIRED_TABLES", "assert_schema_complete"]
