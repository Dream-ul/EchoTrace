"""Persist node plan definitions and task inputs independently from budgets."""

from alembic import op
import sqlalchemy as sa


revision = "0007_node_plan_inputs"
down_revision = "0006_node_driven_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name, column_name in (("task_plans", "definition_json"), ("investigation_tasks", "input_json")):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if column_name in columns:
            continue
        op.add_column(table_name, sa.Column(column_name, sa.JSON(), nullable=True))
        table = sa.table(table_name, sa.column(column_name, sa.JSON()))
        op.execute(table.update().values({column_name: {}}))
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch:
                batch.alter_column(column_name, existing_type=sa.JSON(), nullable=False)
        else:
            op.alter_column(table_name, column_name, existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name, column_name in (("investigation_tasks", "input_json"), ("task_plans", "definition_json")):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table_name)}
        if column_name not in columns:
            continue
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch:
                batch.drop_column(column_name)
        else:
            op.drop_column(table_name, column_name)
