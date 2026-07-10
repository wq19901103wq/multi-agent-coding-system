from contextlib import contextmanager
from typing import Generator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from mac_system.config import get_settings

_settings = get_settings()
_engine = create_engine(
    _settings.database_url,
    echo=_settings.debug,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
)


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
