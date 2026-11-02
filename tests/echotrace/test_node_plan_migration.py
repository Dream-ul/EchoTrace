import importlib
import os
from pathlib import Path
import subprocess
import sys

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


ROOT = Path(__file__).parents[2]


def test_node_plan_migration_backfills_existing_rows_and_downgrades(monkeypatch):
    migration = importlib.import_module("migrations.versions.0007_node_plan_inputs")
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            metadata = sa.MetaData()
            for name in ("task_plans", "investigation_tasks"):
                table = sa.Table(name, metadata, sa.Column("id", sa.Integer(), primary_key=True))
                table.create(connection)
                connection.execute(table.insert().values(id=1))
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            for name, column in (("task_plans", "definition_json"), ("investigation_tasks", "input_json")):
                table = sa.Table(name, sa.MetaData(), autoload_with=connection)
                assert not table.c[column].nullable
                assert connection.scalar(sa.select(table.c[column])) == {}
            migration.upgrade()
            migration.downgrade()
            for name in ("task_plans", "investigation_tasks"):
                assert [column["name"] for column in sa.inspect(connection).get_columns(name)] == ["id"]
                assert connection.scalar(sa.text(f"SELECT id FROM {name}")) == 1
    finally:
        engine.dispose()


def test_alembic_chain_upgrades_and_reapplies_node_plan_migration(tmp_path):
    url = f"sqlite+pysqlite:///{(tmp_path / 'migration.db').as_posix()}"
    env = {**os.environ, "ECHOTRACE_DATABASE_URL": url}
    for operation, target in (("upgrade", "head"), ("downgrade", "0007_node_plan_inputs"), ("upgrade", "head")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), operation, target],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "definition_json" in {column["name"] for column in inspector.get_columns("task_plans")}
            assert "input_json" in {column["name"] for column in inspector.get_columns("investigation_tasks")}
            foreign_keys = {item["name"] for item in inspector.get_foreign_keys("investigation_tasks")}
            assert {"fk_investigation_tasks_node_id", "fk_investigation_tasks_review_id"} <= foreign_keys
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0011_beijing_persistence_time"
    finally:
        engine.dispose()
