"""Add node-driven Trace templates, revisions, evidence and reviews."""

from alembic import op
import sqlalchemy as sa


revision = "0006_node_driven_trace"
down_revision = "0003_expand_canonical_url"
branch_labels = None
depends_on = None


def _options(bind):
    options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    if bind.dialect.name == "mysql":
        parent = bind.execute(sa.text(
            "SELECT TABLE_COLLATION FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'investigation_runs'"
        )).scalar()
        if parent:
            options["mysql_collate"] = str(parent)
    return options


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    options = _options(bind)
    sa.Table("investigation_runs", metadata, sa.Column("run_id", sa.String(36)))
    sa.Table("source_documents", metadata, sa.Column("document_id", sa.String(36)))

    templates = sa.Table(
        "trace_templates", metadata,
        sa.Column("template_key", sa.String(128), primary_key=True),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("template_version", sa.String(128), nullable=False),
        sa.Column("investigation_type", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(1024), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("language", sa.String(128), nullable=False),
        sa.Column("max_node_revisions", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("template_id", "template_version", name="uq_trace_template_version"),
        sa.Index("ix_trace_templates_type", "investigation_type"), **options,
    )
    templates.create(bind=bind, checkfirst=True)
    template_nodes = sa.Table(
        "trace_template_nodes", metadata,
        sa.Column("template_key", sa.String(128), sa.ForeignKey("trace_templates.template_key", ondelete="RESTRICT"), primary_key=True),
        sa.Column("slot_id", sa.String(128), primary_key=True),
        sa.Column("node_order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(1024), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("aspects_json", sa.JSON(), nullable=False),
        sa.Column("claim_types_json", sa.JSON(), nullable=False),
        sa.Column("preferred_sources_json", sa.JSON(), nullable=False),
        sa.Column("research_questions_json", sa.JSON(), nullable=False),
        sa.Column("review_rubric_json", sa.JSON(), nullable=False),
        sa.Column("depends_on_slot_ids_json", sa.JSON(), nullable=False),
        sa.Column("allow_multiple_instances", sa.Boolean(), nullable=False),
        sa.Column("media_policy_json", sa.JSON(), nullable=False),
        sa.Index("ix_trace_template_nodes_order", "template_key", "node_order"), **options,
    )
    template_nodes.create(bind=bind, checkfirst=True)
    template_edges = sa.Table(
        "trace_template_edges", metadata,
        sa.Column("template_key", sa.String(128), sa.ForeignKey("trace_templates.template_key", ondelete="RESTRICT"), primary_key=True),
        sa.Column("edge_order", sa.Integer(), primary_key=True),
        sa.Column("from_slot_id", sa.String(128), nullable=False),
        sa.Column("to_slot_id", sa.String(128), nullable=False),
        sa.Column("relation", sa.String(128), nullable=False),
        sa.Column("label_goal", sa.Text(), nullable=False),
        sa.Column("requires_explicit_support", sa.Boolean(), nullable=False),
        sa.Index("ix_trace_template_edges_from", "template_key", "from_slot_id"), **options,
    )
    template_edges.create(bind=bind, checkfirst=True)

    nodes = sa.Table(
        "investigation_nodes", metadata,
        sa.Column("node_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("investigation_runs.run_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("template_id", sa.String(128), nullable=False),
        sa.Column("template_version", sa.String(128), nullable=False),
        sa.Column("slot_id", sa.String(128), nullable=False),
        sa.Column("node_order", sa.Integer(), nullable=False),
        sa.Column("instance_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(128), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("frozen_revision", sa.Integer()),
        sa.Column("current_review_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "slot_id", "instance_no", name="uq_investigation_node_slot_instance"),
        sa.Index("ix_investigation_nodes_run_status", "run_id", "status"), **options,
    )
    nodes.create(bind=bind, checkfirst=True)
    revisions_table = sa.Table(
        "node_revisions", metadata,
        sa.Column("revision_id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("investigation_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("draft_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("node_id", "revision_no", name="uq_node_revision_no"),
        sa.Index("ix_node_revisions_node_created", "node_id", "created_at"), **options,
    )
    revisions_table.create(bind=bind, checkfirst=True)
    claims = sa.Table(
        "node_claims", metadata,
        sa.Column("claim_id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("node_revisions.revision_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("claim_type", sa.String(128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("object_value", sa.Text(), nullable=False),
        sa.Column("time_scope", sa.Text(), nullable=False),
        sa.Column("source_document_ids_json", sa.JSON(), nullable=False),
        sa.Index("ix_node_claims_revision", "revision_id"), **options,
    )
    claims.create(bind=bind, checkfirst=True)
    evidence = sa.Table(
        "node_evidence", metadata,
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("node_revisions.revision_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("claim_id", sa.String(36), sa.ForeignKey("node_claims.claim_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("source_documents.document_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("locator_json", sa.JSON(), nullable=False),
        sa.Column("relation", sa.String(128), nullable=False),
        sa.Index("ix_node_evidence_revision", "revision_id"),
        sa.Index("ix_node_evidence_claim", "claim_id"), **options,
    )
    evidence.create(bind=bind, checkfirst=True)
    media = sa.Table(
        "node_media", metadata,
        sa.Column("media_id", sa.String(36), primary_key=True),
        sa.Column("revision_id", sa.String(36), sa.ForeignKey("node_revisions.revision_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("source_documents.document_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("object_key", sa.String(512)),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("why_relevant", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("captured_at", sa.DateTime(timezone=True)), **options,
    )
    media.create(bind=bind, checkfirst=True)
    reviews = sa.Table(
        "node_reviews", metadata,
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("investigation_nodes.node_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(128), nullable=False),
        sa.Column("covered_aspects_json", sa.JSON(), nullable=False),
        sa.Column("missing_aspects_json", sa.JSON(), nullable=False),
        sa.Column("redundant_claims_json", sa.JSON(), nullable=False),
        sa.Column("unclear_claims_json", sa.JSON(), nullable=False),
        sa.Column("evidence_followups_json", sa.JSON(), nullable=False),
        sa.Column("rewrite_instructions_json", sa.JSON(), nullable=False),
        sa.Column("next_research_tasks_json", sa.JSON(), nullable=False),
        sa.Column("final_editor_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("node_id", "revision_no", name="uq_node_review_revision"),
        sa.Index("ix_node_reviews_node_created", "node_id", "created_at"), **options,
    )
    reviews.create(bind=bind, checkfirst=True)

    run_columns = {column["name"] for column in sa.inspect(bind).get_columns("investigation_runs")}
    if "template_id" not in run_columns:
        op.add_column("investigation_runs", sa.Column("template_id", sa.String(128), nullable=True))
    if "template_version" not in run_columns:
        op.add_column("investigation_runs", sa.Column("template_version", sa.String(128), nullable=True))
    task_columns = {column["name"] for column in sa.inspect(bind).get_columns("investigation_tasks")}
    for name, column in (
        ("node_id", sa.Column("node_id", sa.String(36), nullable=True)),
        ("slot_id", sa.Column("slot_id", sa.String(128), nullable=True)),
        ("node_revision", sa.Column("node_revision", sa.Integer(), nullable=True)),
        ("review_id", sa.Column("review_id", sa.String(128), nullable=True)),
    ):
        if name not in task_columns:
            op.add_column("investigation_tasks", column)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("node_reviews", "node_media", "node_evidence", "node_claims", "node_revisions", "investigation_nodes", "trace_template_edges", "trace_template_nodes", "trace_templates"):
        if table in sa.inspect(bind).get_table_names():
            op.drop_table(table)
    task_columns = {column["name"] for column in sa.inspect(bind).get_columns("investigation_tasks")}
    for name in ("review_id", "node_revision", "slot_id", "node_id"):
        if name in task_columns:
            op.drop_column("investigation_tasks", name)
    run_columns = {column["name"] for column in sa.inspect(bind).get_columns("investigation_runs")}
    for name in ("template_version", "template_id"):
        if name in run_columns:
            op.drop_column("investigation_runs", name)
