"""Align the development database with the current run snapshot contract.

The bootstrap migration can predate the report snapshot field when it is run
against a database created from an older metadata revision.  The application
reads this field on every run lookup, so the schema must be repaired
explicitly instead of relying on ``create_all``.
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_align_run_report_column"
down_revision = "0009_remove_legacy_ledgers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("investigation_runs")}
    if "report_document_json" in columns:
        return
    if bind.dialect.name == "mysql":
        op.add_column("investigation_runs", sa.Column("report_document_json", sa.JSON(), nullable=True))
        op.execute(sa.text("UPDATE investigation_runs SET report_document_json = :empty WHERE report_document_json IS NULL").bindparams(empty="{}"))
        op.execute(sa.text("ALTER TABLE investigation_runs MODIFY COLUMN report_document_json JSON NOT NULL"))
    else:
        op.add_column(
            "investigation_runs",
            sa.Column("report_document_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("investigation_runs")}
    if "report_document_json" in columns:
        op.drop_column("investigation_runs", "report_document_json")
