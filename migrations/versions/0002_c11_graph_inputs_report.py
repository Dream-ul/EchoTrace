"""Node workflow metadata and persisted ReportDocument snapshot."""

from alembic import op
import sqlalchemy as sa

revision = "0002_c11_graph_inputs_report"
down_revision = "0001_c10_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The bootstrap revision imports the current SQLAlchemy metadata for
    # portability, so a fresh database may already contain these additive
    # columns.  Make the revision safe in both states (fresh bootstrap and a
    # database at the preceding migration) instead of failing with duplicate
    # columns.
    bind = op.get_bind()
    dialect = bind.dialect.name
    existing = {column["name"] for column in sa.inspect(bind).get_columns("investigation_runs")}
    json_column = lambda name: sa.Column(
        name,
        sa.JSON(),
        nullable=(dialect == "mysql"),
        server_default=None if dialect == "mysql" else sa.text("'{}'"),
    )
    additions = (
        ("investigation_type", sa.Column("investigation_type", sa.String(length=64), nullable=False, server_default="auto")),
        ("subject", sa.Column("subject", sa.String(length=512), nullable=True)),
        ("scope_json", json_column("scope_json")),
        ("report_document_json", json_column("report_document_json")),
        ("graph_state_json", json_column("graph_state_json")),
    )
    added_json = []
    for name, column in additions:
        if name not in existing:
            op.add_column("investigation_runs", column)
            if name.endswith("_json"):
                added_json.append(name)
    if dialect == "mysql" and added_json:
        # MySQL/MariaDB versions used in local deployments do not allow a
        # JSON DEFAULT at all.  Add nullable, backfill, then tighten the
        # column without a default.
        for name in added_json:
            op.execute(sa.text(f"UPDATE investigation_runs SET {name} = :empty WHERE {name} IS NULL").bindparams(empty="{}"))
            op.execute(sa.text(f"ALTER TABLE investigation_runs MODIFY COLUMN {name} JSON NOT NULL"))
    if dialect not in {"sqlite", "mysql"}:
        for name in ("investigation_type", "scope_json", "report_document_json", "graph_state_json"):
            # Removing the temporary default keeps application writes explicit.
            op.alter_column("investigation_runs", name, server_default=None)


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("investigation_runs")}
    for name in ("graph_state_json", "report_document_json", "scope_json", "subject", "investigation_type"):
        if name in existing:
            op.drop_column("investigation_runs", name)
