from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(
    f"sqlite:///{settings.database_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


_DATABASE_CONNECTION_COLUMNS = {
    "tunnel_url": "VARCHAR(1000)",
    "tunnel_username": "VARCHAR(255)",
    "tunnel_password_encrypted": "TEXT",
    "tunnel_encode_base64": "BOOLEAN NOT NULL DEFAULT 1",
}


def _migrate_database_connections() -> None:
    with engine.begin() as connection:
        existing = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(database_connections)"
            )
        }
        for name, ddl in _DATABASE_CONNECTION_COLUMNS.items():
            if name not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE database_connections ADD COLUMN {name} {ddl}"
                )


def init_db() -> None:
    from app import models  # noqa: F401  (register mappings)

    Base.metadata.create_all(engine)
    _migrate_database_connections()
    _prune_operation_logs()


def _prune_operation_logs() -> None:
    from app.services.operation_logs import prune_expired

    try:
        prune_expired()
    except Exception:  # 保留期清理不该阻断启动
        logging.getLogger(__name__).exception("清理过期操作日志失败")