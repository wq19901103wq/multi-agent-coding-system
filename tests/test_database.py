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
        conn.execute(
            text(
                "CREATE TABLE project_decisions ("
                "id VARCHAR PRIMARY KEY, project_id VARCHAR)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO project_decisions (id, project_id) "
                "VALUES ('decision-old', 'project-old')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE project_owner_states (project_id VARCHAR PRIMARY KEY)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO project_owner_states (project_id) "
                "VALUES ('project-old')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE project_owner_reviews ("
                "id VARCHAR PRIMARY KEY, project_id VARCHAR, "
                "request_item_id VARCHAR, created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO project_owner_reviews "
                "(id, project_id, request_item_id, created_at) VALUES "
                "('review-old', 'project-old', 'report:old', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE user_owner_memories ("
                "id VARCHAR PRIMARY KEY, open_id VARCHAR, project_id VARCHAR, "
                "scope VARCHAR, content VARCHAR, evidence VARCHAR, "
                "source_ref VARCHAR, created_at DATETIME, updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO user_owner_memories "
                "(id, open_id, project_id, scope, content, evidence, source_ref, "
                "created_at, updated_at) VALUES "
                "('memory-old', 'ou-old', NULL, 'global', '偏好', '原话', "
                "'report:old', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    _run_migrations(engine)

    with engine.connect() as conn:
        plan = conn.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM event_logs WHERE "
                "json_extract(meta, '$.profile_type') = 'root_agent_event' "
                "AND json_extract(meta, '$.kind') = 'thread/tokenUsage/updated'"
            )
        ).all()
    assert any("ix_event_logs_native_prune" in row[3] for row in plan)

    columns = {c["name"] for c in inspect(engine).get_columns("projects")}
    assert "feishu_chat_id" in columns
    assert "feishu_chat_type" in columns
    assert "context_version" in columns

    tables = inspect(engine).get_table_names()
    assert "pending_intents" in tables
    assert "processed_events" in tables
    assert "project_users" in tables
    assert "notification_outbox" in tables
    assert "root_agent_sessions" in tables
    assert "dynamic_tool_calls" in tables
    assert "project_owner_states" in tables
    assert "project_policies" in tables
    assert "user_owner_memories" in tables
    assert "user_owner_sources" in tables
    assert "user_identity_links" in tables

    owner_memory_columns = {
        c["name"] for c in inspect(engine).get_columns("user_owner_memories")
    }
    assert "human_id" in owner_memory_columns
    owner_source_columns = {
        c["name"] for c in inspect(engine).get_columns("user_owner_sources")
    }
    assert "human_id" in owner_source_columns
    assert "source_project_id" in owner_memory_columns
    assert {
        "confidence", "learning_type", "reinforcement_count", "source_refs"
    }.issubset(owner_memory_columns)

    root_session_columns = {
        c["name"] for c in inspect(engine).get_columns("root_agent_sessions")
    }
    assert {"last_request_origin", "last_parent_decision_id"}.issubset(
        root_session_columns
    )

    decision_columns = {
        c["name"] for c in inspect(engine).get_columns("project_decisions")
    }
    assert {
        "decision_type",
        "parent_decision_id",
        "execution_status",
        "execution_rounds",
        "acceptance_criteria",
        "result_refs",
        "authority_refs",
    }.issubset(decision_columns)

    projection_columns = {
        c["name"] for c in inspect(engine).get_columns("card_projections")
    }
    assert "view_state" in projection_columns

    review_columns = {
        c["name"] for c in inspect(engine).get_columns("project_owner_reviews")
    }
    assert {
        "request_origin",
        "parent_decision_id",
        "continue_execution",
        "memory_updates",
        "policy_updates",
        "authority_refs",
    }.issubset(review_columns)

    policy_columns = {
        c["name"] for c in inspect(engine).get_columns("project_policies")
    }
    assert {
        "source_time",
        "source_context",
        "supersedes_refs",
    }.issubset(policy_columns)

    owner_state_columns = {
        c["name"] for c in inspect(engine).get_columns("project_owner_states")
    }
    assert {
        "enabled",
        "owner_open_id",
        "version",
    }.issubset(owner_state_columns)
    with engine.connect() as conn:
        old_decision = conn.execute(
            text(
                "SELECT acceptance_criteria, result_refs, authority_refs "
                "FROM project_decisions WHERE id = 'decision-old'"
            )
        ).one()
        assert tuple(old_decision) == ("[]", "[]", "[]")
        assert conn.execute(
            text(
                "SELECT source_project_id FROM user_owner_memories "
                "WHERE id = 'memory-old'"
            )
        ).scalar_one() == "project-old"
        assert conn.execute(
            text(
                "SELECT learning_type FROM user_owner_memories "
                "WHERE id = 'memory-old'"
            )
        ).scalar_one() == "explicit"

    task_columns = {c["name"] for c in inspect(engine).get_columns("tasks")}
    assert "input_data" in task_columns
    assert "output_data" in task_columns
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
