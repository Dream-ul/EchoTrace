"""Store EchoTrace persistence timestamps as Asia/Shanghai wall-clock values.

MySQL ``DATETIME`` does not retain an offset. Before this migration the
application wrote UTC-aware values, which MySQL stored as UTC wall-clock text.
Existing development rows therefore need one explicit eight-hour conversion;
new rows use the Beijing clock in the application models.
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_beijing_persistence_time"
down_revision = "0010_align_run_report_column"
branch_labels = None
depends_on = None


_TIMESTAMP_COLUMNS = {
    "source_documents": ("published_at", "fetched_at"),
    "source_clusters": ("created_at",),
    "analysis_features": ("created_at",),
    "trace_templates": ("created_at",),
    "investigation_nodes": ("created_at", "updated_at"),
    "node_revisions": ("created_at",),
    "node_media": ("published_at", "captured_at"),
    "node_reviews": ("created_at",),
    "investigation_runs": ("created_at", "updated_at"),
    "events": ("first_seen_at", "last_seen_at", "created_at", "updated_at"),
    "event_documents": ("created_at",),
    "run_events": ("created_at",),
    "run_checkpoints": ("created_at",),
    "task_plans": ("created_at", "updated_at"),
    "investigation_tasks": ("created_at", "updated_at", "lease_until"),
    "task_attempts": ("started_at", "heartbeat_at", "finished_at"),
    "tool_calls": ("created_at", "finished_at"),
    "agent_skill_activations": ("activated_at",),
    "agent_skill_definitions": ("loaded_at",),
    "tool_provider_definitions": ("loaded_at",),
    "outbox_events": ("available_at", "locked_until", "published_at", "created_at"),
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _TIMESTAMP_COLUMNS.items():
        if table not in tables:
            continue
        available = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column in available:
                op.execute(sa.text(
                    f"UPDATE `{table}` SET `{column}` = DATE_ADD(`{column}`, INTERVAL 8 HOUR) "
                    f"WHERE `{column}` IS NOT NULL"
                ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, columns in _TIMESTAMP_COLUMNS.items():
        if table not in tables:
            continue
        available = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column in available:
                op.execute(sa.text(
                    f"UPDATE `{table}` SET `{column}` = DATE_SUB(`{column}`, INTERVAL 8 HOUR) "
                    f"WHERE `{column}` IS NOT NULL"
                ))
