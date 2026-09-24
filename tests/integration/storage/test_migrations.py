from io import StringIO
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
import pytest
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
    "planning_rounds",
    "budget_stops",
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


def test_tool_attempt_migration_backfills_existing_calls_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "backfill.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260922_0001")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO diagnosis_tasks (id, user_query, namespace, status) "
            "VALUES ('task_001', 'diagnose api', 'team-a', 'PLANNING')"
        ))
        connection.execute(text(
            "INSERT INTO agent_runs (id, task_id, trace_id, attempt_no, status, state_payload, runtime_version) "
            "VALUES ('run_001', 'task_001', 'trace_001', 1, 'PLANNING', '{}', 'v1')"
        ))
        connection.execute(text(
            "INSERT INTO tool_calls (id, run_id, sequence_no, tool_name, tool_version, risk_level, arguments_payload) "
            "VALUES ('tool_legacy', 'run_001', 1, 'k8s.get_pod_status', 'v1', 0, '{}')"
        ))

    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT logical_call_id, attempt_no FROM tool_calls WHERE id = 'tool_legacy'"
        )).one()
        assert row == ("tool_legacy", 1)
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO tool_calls (id, run_id, sequence_no, logical_call_id, attempt_no, "
                "tool_name, tool_version, risk_level, arguments_payload) "
                "VALUES ('tool_duplicate', 'run_001', 2, 'tool_legacy', 1, "
                "'k8s.get_pod_status', 'v1', 0, '{}')"
            ))

    command.downgrade(config, "20260922_0001")
    columns = {item["name"] for item in inspect(engine).get_columns("tool_calls")}
    assert "logical_call_id" not in columns
    assert "attempt_no" not in columns
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT logical_call_id, attempt_no FROM tool_calls WHERE id = 'tool_legacy'"
        )).one()
        assert row == ("tool_legacy", 1)
    engine.dispose()


def test_v2_round_migration_preserves_existing_v1_run(tmp_path: Path) -> None:
    database_path = tmp_path / "v1-compat.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260923_0003")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO diagnosis_tasks (id, user_query, namespace, status) "
            "VALUES ('task_legacy', 'diagnose api', 'team-a', 'COMPLETED')"
        ))
        connection.execute(text(
            "INSERT INTO agent_runs (id, task_id, trace_id, attempt_no, status, state_payload, runtime_version) "
            "VALUES ('run_legacy', 'task_legacy', 'trace_legacy', 1, 'COMPLETED', '{}', 'v1')"
        ))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT runtime_version, status FROM agent_runs WHERE id = 'run_legacy'"
        )).one() == ("v1", "COMPLETED")
        assert connection.execute(text("SELECT count(*) FROM planning_rounds")).scalar_one() == 0
    engine.dispose()


def test_budget_stop_migration_is_additive_and_reversible(tmp_path: Path) -> None:
    database_path = tmp_path / "budget-stop-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260923_0004")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO diagnosis_tasks (id, user_query, namespace, status) "
            "VALUES ('task_old', 'diagnose api', 'team-a', 'PARTIAL')"
        ))
        connection.execute(text(
            "INSERT INTO agent_runs (id, task_id, trace_id, attempt_no, status, "
            "state_payload, runtime_version) "
            "VALUES ('run_old', 'task_old', 'trace_old', 1, 'PARTIAL', '{}', 'v1')"
        ))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM budget_stops")).scalar_one() == 0
        assert connection.execute(text(
            "SELECT trace_id, runtime_version FROM agent_runs WHERE id='run_old'"
        )).one() == ("trace_old", "v1")
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO budget_stops (run_id, dimension, phase, step_no, kind, "
                "requested, budget_payload, created_at) VALUES "
                "('run_old', 'invalid', 'planner.before', 1, 'exhausted', 0, '{}', CURRENT_TIMESTAMP)"
            ))
    command.downgrade(config, "20260923_0004")
    assert "budget_stops" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT id FROM agent_runs WHERE id='run_old'"
        )).scalar_one() == "run_old"
    command.upgrade(config, "head")
    assert "budget_stops" in inspect(engine).get_table_names()
    engine.dispose()
