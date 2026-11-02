"""Allow full canonical public URLs without lossy truncation.

Search providers may return signed or tracking URLs longer than the old
VARCHAR(255) field. The hash remains the indexed lookup key, while the raw URL
is retained verbatim in TEXT for reproducibility.
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_expand_canonical_url"
down_revision = "0002_c11_graph_inputs_report"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"]: column for column in sa.inspect(bind).get_columns("source_documents")}
    if "canonical_url" not in columns:
        return
    if bind.dialect.name == "mysql":
        op.execute("ALTER TABLE source_documents MODIFY COLUMN canonical_url TEXT NULL")
    else:
        with op.batch_alter_table("source_documents") as batch:
            batch.alter_column("canonical_url", existing_type=sa.String(length=255), type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # Refuse a lossy downgrade when any persisted URL needs the TEXT field.
        too_long = bind.execute(sa.text("SELECT COUNT(*) FROM source_documents WHERE CHAR_LENGTH(canonical_url) > 255")).scalar() or 0
        if too_long:
            raise RuntimeError("cannot downgrade canonical_url: persisted URLs exceed 255 characters")
        op.execute("ALTER TABLE source_documents MODIFY COLUMN canonical_url VARCHAR(255) NULL")
    else:
        with op.batch_alter_table("source_documents") as batch:
            batch.alter_column("canonical_url", existing_type=sa.Text(), type_=sa.String(length=255), existing_nullable=True)
