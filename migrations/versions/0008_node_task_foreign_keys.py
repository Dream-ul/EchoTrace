"""Add durable foreign keys from node tasks to node and review records."""

from alembic import op
import sqlalchemy as sa


revision = "0008_node_task_foreign_keys"
down_revision = "0007_node_plan_inputs"
branch_labels = None
depends_on = None


def _existing(bind) -> set[str | None]:
    return {item.get("name") for item in sa.inspect(bind).get_foreign_keys("investigation_tasks")}


def upgrade() -> None:
    bind = op.get_bind()
    names = _existing(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("investigation_tasks") as batch:
            if "fk_investigation_tasks_node_id" not in names:
                batch.create_foreign_key(
                    "fk_investigation_tasks_node_id", "investigation_nodes",
                    ["node_id"], ["node_id"], ondelete="RESTRICT",
                )
            if "fk_investigation_tasks_review_id" not in names:
                batch.create_foreign_key(
                    "fk_investigation_tasks_review_id", "node_reviews",
                    ["review_id"], ["review_id"], ondelete="RESTRICT",
                )
    else:
        if "fk_investigation_tasks_node_id" not in names:
            op.create_foreign_key(
                "fk_investigation_tasks_node_id", "investigation_tasks", "investigation_nodes",
                ["node_id"], ["node_id"], ondelete="RESTRICT",
            )
        if "fk_investigation_tasks_review_id" not in names:
            op.create_foreign_key(
                "fk_investigation_tasks_review_id", "investigation_tasks", "node_reviews",
                ["review_id"], ["review_id"], ondelete="RESTRICT",
            )


def downgrade() -> None:
    bind = op.get_bind()
    names = _existing(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("investigation_tasks") as batch:
            for name in ("fk_investigation_tasks_review_id", "fk_investigation_tasks_node_id"):
                if name in names:
                    batch.drop_constraint(name, type_="foreignkey")
    else:
        for name in ("fk_investigation_tasks_review_id", "fk_investigation_tasks_node_id"):
            if name in names:
                op.drop_constraint(name, "investigation_tasks", type_="foreignkey")
