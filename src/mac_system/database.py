from contextlib import contextmanager
from typing import Generator

from sqlmodel import Session, SQLModel, create_engine

from mac_system.config import get_settings

_settings = get_settings()
_engine = create_engine(
    _settings.database_url,
    echo=_settings.debug,
    connect_args={"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {},
)


def init_db() -> None:
    SQLModel.metadata.create_all(_engine)


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
