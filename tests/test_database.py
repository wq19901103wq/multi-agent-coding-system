from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base

from mac_system.database import _engine, _run_migrations


Base = declarative_base()


def test_sqlite_engine_waits_for_writes_and_uses_wal():
    with _engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() == 30000
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_migrations_add_missing_columns_and_tables(tmp_path: Path):
    """Existing SQLite DBs get new columns/tables without manual migration."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # Simulate a pre-existing projects table from before Feishu columns were added.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE projects (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR,
                    task_type VARCHAR NOT NULL,
                    description VARCHAR,
                    status VARCHAR,
                    current_state VARCHAR NOT NULL,
                    previous_state VARCHAR,
                    workspace_path VARCHAR NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME,
                    context JSON,
                    approval_config JSON,
                    total_cost_usd FLOAT
                )
                """
            )
        )

    _run_migrations(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("projects")}
    assert "feishu_chat_id" in columns
    assert "feishu_chat_type" in columns
    assert "context_version" in columns

    tables = inspect(engine).get_table_names()
    assert "pending_intents" in tables
    assert "processed_events" in tables
    assert "project_users" in tables
    assert "project_context_records" in tables
    assert "notification_outbox" in tables

    task_columns = {c["name"] for c in inspect(engine).get_columns("tasks")}
    assert "context_version" in task_columns
    assert "attempt_id" in task_columns
    assert "attempt_number" in task_columns
    assert "superseded_by_task_id" in task_columns
    assert "cancellation_reason" in task_columns

    assert "task_failure_reports" in tables
    assert "acceptance_evidence" in tables
    assert "execution_status" in columns
    assert "waiting_reason" in columns
    assert "waiting_since" in columns

    interaction_columns = {
        c["name"] for c in inspect(engine).get_columns("question_interactions")
    }
    assert "requested_to" in interaction_columns
    assert "escalated_to_user" in interaction_columns
    assert "resolution_source" in interaction_columns

    answer_columns = {
        c["name"] for c in inspect(engine).get_columns("question_answers")
    }
    assert "answer_source" in answer_columns
    assert "source_refs" in answer_columns

    question_columns = {c["name"] for c in inspect(engine).get_columns("questions")}
    assert "sensitivity" in question_columns
    assert "secret_env_var" in question_columns
