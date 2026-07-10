from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base

from mac_system.database import _run_migrations


Base = declarative_base()


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

    tables = inspect(engine).get_table_names()
    assert "pending_intents" in tables
    assert "processed_events" in tables
    assert "project_users" in tables
