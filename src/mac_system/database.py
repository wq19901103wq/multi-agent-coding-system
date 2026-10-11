from contextlib import contextmanager
from typing import Generator

from sqlalchemy import event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from mac_system.config import get_settings

# Import models so SQLModel.metadata is populated for migrations and create_all.
import mac_system.models  # noqa: F401

_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")
_engine = create_engine(
    _settings.database_url,
    echo=_settings.debug,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)


if _is_sqlite:
    @event.listens_for(_engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        """Allow concurrent readers and wait briefly for the single writer."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _sqlite_column_def(column) -> str:
    """Build an SQLite ALTER TABLE ADD COLUMN fragment for ``column``."""
    from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect

    dialect = sqlite_dialect()
    col_type = column.type.compile(dialect=dialect)
    default_clause = ""
    if column.server_default is not None:
        default_clause = f" DEFAULT {column.server_default.arg}"
    elif column.default is not None:
        arg = getattr(column.default, "arg", None)
        if arg is not None and not callable(arg):
            default_clause = f" DEFAULT {repr(arg)}"
    return f"{column.name} {col_type}{default_clause}"


def _run_migrations(engine) -> None:
    """Create missing tables and add missing columns.

    For SQLite we add columns with ALTER TABLE. Other engines get table
    creation only; full migrations can be added later via Alembic.
    """
    import logging

    logger = logging.getLogger(__name__)

    SQLModel.metadata.create_all(engine)

    if engine.dialect.name != "sqlite":
        logger.warning("Non-SQLite engine: column-level migrations are not implemented.")
        return

    inspector = inspect(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_event_logs_native_prune "
                "ON event_logs ("
                "json_extract(meta, '$.profile_type'), "
                "json_extract(meta, '$.kind'), project_id)"
            )
        )
    for table in SQLModel.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table.name} ADD COLUMN {_sqlite_column_def(column)}"
                        )
                    )
                logger.info("Added column %s.%s", table.name, column.name)
            except Exception as exc:
                logger.warning(
                    "Failed to add column %s.%s: %s", table.name, column.name, exc
                )

    # SQLite cannot attach callable JSON defaults through ``ALTER TABLE``.
    # Backfill only the newly introduced Owner memory fields so rows created
    # by older MAC versions keep the same list semantics as new rows.
    owner_list_columns = {
        "project_decisions": (
            "acceptance_criteria",
            "result_refs",
            "authority_refs",
        ),
        "project_policies": ("supersedes_refs",),
        "project_owner_reviews": (
            "memory_updates",
            "policy_updates",
            "authority_refs",
        ),
        "user_owner_memories": ("source_refs",),
    }
    refreshed = inspect(engine)
    with engine.begin() as conn:
        for table_name, column_names in owner_list_columns.items():
            if not refreshed.has_table(table_name):
                continue
            existing = {
                item["name"] for item in refreshed.get_columns(table_name)
            }
            for column_name in column_names:
                if column_name in existing:
                    conn.execute(
                        text(
                            f"UPDATE {table_name} SET {column_name} = '[]' "
                            f"WHERE {column_name} IS NULL"
                        )
                    )
        if refreshed.has_table("project_policies"):
            policy_columns = {
                item["name"] for item in refreshed.get_columns("project_policies")
            }
            if "source_context" in policy_columns:
                conn.execute(
                    text(
                        "UPDATE project_policies SET source_context = '{}' "
                        "WHERE source_context IS NULL"
                    )
                )
        for table_name in ("user_owner_memories", "user_owner_sources"):
            if not refreshed.has_table(table_name):
                continue
            columns = {
                item["name"] for item in refreshed.get_columns(table_name)
            }
            if {"human_id", "bot_account_id", "open_id"}.issubset(columns):
                conn.execute(
                    text(
                        f"UPDATE {table_name} "
                        "SET human_id = 'feishu-open:' || "
                        "COALESCE(bot_account_id, 'default') || ':' || open_id "
                        "WHERE human_id IS NULL OR human_id = ''"
                    )
                )
        # Older global Owner memories kept only a public review reference;
        # recover the source project where that review is still available.
        if refreshed.has_table("user_owner_memories") and refreshed.has_table(
            "project_owner_reviews"
        ):
            memory_columns = {
                item["name"]
                for item in refreshed.get_columns("user_owner_memories")
            }
            if "source_project_id" in memory_columns:
                conn.execute(
                    text(
                        "UPDATE user_owner_memories "
                        "SET source_project_id = COALESCE("
                        "source_project_id, project_id, ("
                        "SELECT project_owner_reviews.project_id "
                        "FROM project_owner_reviews "
                        "WHERE project_owner_reviews.request_item_id = "
                        "user_owner_memories.source_ref "
                        "ORDER BY project_owner_reviews.created_at DESC LIMIT 1"
                        ")) WHERE source_project_id IS NULL"
                    )
                )


def init_db() -> None:
    _run_migrations(_engine)


def get_session() -> Generator[Session, None, None]:
    with Session(_engine) as session:
        yield session


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = Session(_engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
