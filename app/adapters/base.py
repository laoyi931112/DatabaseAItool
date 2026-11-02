from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.config import settings
from app.models import DatabaseConnection
from app.security import decrypt_secret

DB_TYPE_INFO: dict[str, dict[str, Any]] = {
    "mysql": {
        "label": "MySQL / MariaDB",
        "kind": "sql",
        "default_port": 3306,
        "database_label": "数据库名",
        "needs_host": True,
        "needs_auth": True,
    },
    "postgresql": {
        "label": "PostgreSQL",
        "kind": "sql",
        "default_port": 5432,
        "database_label": "数据库名",
        "needs_host": True,
        "needs_auth": True,
    },
    "sqlite": {
        "label": "SQLite",
        "kind": "sql",
        "default_port": None,
        "database_label": "数据库文件绝对路径",
        "needs_host": False,
        "needs_auth": False,
    },
    "mssql": {
        "label": "SQL Server",
        "kind": "sql",
        "default_port": 1433,
        "database_label": "数据库名",
        "needs_host": True,
        "needs_auth": True,
    },
    "oracle": {
        "label": "Oracle",
        "kind": "sql",
        "default_port": 1521,
        "database_label": "服务名 (Service Name)",
        "needs_host": True,
        "needs_auth": True,
    },
    "mongodb": {
        "label": "MongoDB",
        "kind": "mongo",
        "default_port": 27017,
        "database_label": "数据库名",
        "needs_host": True,
        "needs_auth": True,
    },
}


class AdapterError(Exception):
    """Raised when a database operation fails or is not allowed."""


@dataclass(frozen=True)
class DBConfig:
    id: int
    name: str
    db_type: str
    database: str
    host: str | None
    port: int | None
    username: str | None
    password: str | None
    options: dict[str, Any]
    max_rows: int
    query_timeout: int

    @classmethod
    def from_model(cls, model: DatabaseConnection) -> "DBConfig":
        try:
            options = json.loads(model.options_json or "{}")
        except json.JSONDecodeError as exc:
            raise AdapterError(f"高级选项不是合法 JSON：{exc.msg}") from exc
        if not isinstance(options, dict):
            raise AdapterError("高级选项必须是 JSON 对象")

        return cls(
            id=model.id,
            name=model.name,
            db_type=model.db_type,
            database=model.database,
            host=model.host,
            port=model.port,
            username=model.username,
            password=decrypt_secret(model.password_encrypted),
            options=options,
            max_rows=model.max_rows or settings.default_max_rows,
            query_timeout=model.query_timeout or settings.default_query_timeout,
        )


def clamp_limit(config: DBConfig, requested: int | None) -> int:
    cap = max(1, min(config.max_rows, settings.hard_max_rows))
    if requested is None:
        return cap
    try:
        requested = int(requested)
    except (TypeError, ValueError) as exc:
        raise AdapterError("max_rows 必须是整数") from exc
    return max(1, min(requested, cap))


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return str(value)


def safe_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message[:800]}"
