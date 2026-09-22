from io import StringIO
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from opspilot.storage import Base

PROJECT_ROOT = Path(__file__).parents[3]
CORE_TABLES = {
    "diagnosis_tasks",
    "agent_runs",
    "llm_calls",
    "tool_calls",
    "evidence",
    "diagnosis_results",
    "prompt_versions",
}


def migration_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_initial_migration_upgrades_downgrades_and_reapplies(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration-test.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert CORE_TABLES.issubset(set(inspect(engine).get_table_names()))
    with engine.connect() as connection:
        migration_context = MigrationContext.configure(connection)
        assert compare_metadata(migration_context, Base.metadata) == []

    command.downgrade(config, "base")
    assert CORE_TABLES.isdisjoint(set(inspect(engine).get_table_names()))

    command.upgrade(config, "head")
    assert CORE_TABLES.issubset(set(inspect(engine).get_table_names()))
    engine.dispose()


def test_all_models_compile_to_postgresql_ddl() -> None:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]

    statements = [
        str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    ]

    assert len(statements) == len(CORE_TABLES)
    assert all("CREATE TABLE" in statement for statement in statements)
    assert any("JSON" in statement for statement in statements)


def test_postgresql_migration_installs_append_only_evidence_trigger() -> None:
    output = StringIO()
    config = migration_config("postgresql+psycopg://user:password@localhost/opspilot")
    config.attributes["output_buffer"] = output

    command.upgrade(config, "head", sql=True)

    migration_sql = output.getvalue()
    assert "CREATE FUNCTION opspilot_reject_evidence_mutation()" in migration_sql
    assert "CREATE TRIGGER trg_evidence_append_only" in migration_sql
    assert "BEFORE UPDATE OR DELETE ON evidence" in migration_sql
