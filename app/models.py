from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.utils import utcnow

key_database_permissions = Table(
    "key_database_permissions",
    Base.metadata,
    Column(
        "api_key_id",
        ForeignKey("api_keys.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "database_id",
        ForeignKey("database_connections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class DatabaseConnection(Base):
    __tablename__ = "database_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    db_type: Mapped[str] = mapped_column(String(30), index=True)
    host: Mapped[str | None] = mapped_column(String(255), default=None)
    port: Mapped[int | None] = mapped_column(Integer, default=None)
    database: Mapped[str] = mapped_column(String(1000))
    username: Mapped[str | None] = mapped_column(String(255), default=None)
    password_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    description: Mapped[str | None] = mapped_column(Text, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_rows: Mapped[int] = mapped_column(Integer, default=500)
    query_timeout: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    api_keys: Mapped[list[ApiKey]] = relationship(
        secondary=key_database_permissions,
        back_populates="databases",
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    key_prefix: Mapped[str] = mapped_column(String(20), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    databases: Mapped[list[DatabaseConnection]] = relationship(
        secondary=key_database_permissions,
        back_populates="api_keys",
        lazy="selectin",
    )

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= utcnow()
