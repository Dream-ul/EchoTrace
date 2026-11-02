"""Remove pre-node global ledgers and snapshot read models.

The project is still in development and existing rows are disposable test
data.  The node revision tables are the only investigation fact source, so
the old global claims/evidence/assessment and Trace snapshot tables are
removed in one destructive migration.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_remove_legacy_ledgers"
down_revision = "0008_node_task_foreign_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "analysis_features" in set(sa.inspect(bind).get_table_names()):
        foreign_keys = sa.inspect(bind).get_foreign_keys("analysis_features")
        for foreign_key in foreign_keys:
            if "claim_id" in (foreign_key.get("constrained_columns") or ()) and foreign_key.get("name"):
                op.drop_constraint(foreign_key["name"], "analysis_features", type_="foreignkey")

        analysis_columns = {column["name"] for column in sa.inspect(bind).get_columns("analysis_features")}
        if "claim_id" in analysis_columns:
            op.drop_column("analysis_features", "claim_id")

    tables = set(sa.inspect(bind).get_table_names())
    for name in (
        "trace_media_assets",
        "trace_source_anchors",
        "trace_edges",
        "trace_nodes",
        "trace_phases",
        "trace_snapshots",
        "assessment_revision_claims",
        "assessment_revisions",
        "evidence_links",
        "claims",
    ):
        if name in tables:
            op.drop_table(name)

    run_columns = {column["name"] for column in sa.inspect(bind).get_columns("investigation_runs")}
    for name in ("last_gate_decision", "report_ir_json"):
        if name in run_columns:
            op.drop_column("investigation_runs", name)


def downgrade() -> None:
    # Historical test databases may move the Alembic marker backwards while
    # retaining the destructive schema.  Recreating the deleted ledgers would
    # reintroduce the architecture this migration removes.
    return None
