"""Initial node-driven persistence schema.

The metadata is the single physical contract.  Using ``create_all`` here keeps
the bootstrap revision in lockstep with the current model set; subsequent
schema changes use explicit Alembic operations.
"""

from alembic import op

from echotrace.infrastructure.persistence.mysql.models import Base

revision = "0001_c10_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    # MySQL cannot compile ``drop_all`` for an unnamed FK constraint emitted
    # by the bootstrap metadata (the dialect receives a ``None`` name).  This
    # revision is a one-shot bootstrap, so use explicit table names and let the
    # server handle the FK graph while checks are disabled for this rollback.
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        Base.metadata.drop_all(bind)
        return
    table_names = list(reversed(Base.metadata.sorted_tables))
    bind.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
    try:
        for table in table_names:
            bind.exec_driver_sql(f"DROP TABLE IF EXISTS `{table.name}`")
    finally:
        bind.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
