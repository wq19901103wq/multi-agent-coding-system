"""Database initialization and session management."""

from sqlmodel import create_engine, Session, SQLModel

from mac_system.config import settings


# Create engine
engine = create_engine(
    settings.database_url,
    echo=settings.mcs_log_level == "debug",
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


def init_database() -> None:
    """Create all tables if they don't exist."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """Get a new database session."""
    return Session(engine)


def get_engine():
    """Get the SQLAlchemy engine."""
    return engine
