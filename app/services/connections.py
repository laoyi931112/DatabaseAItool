from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters import DB_TYPE_INFO, AdapterError, DBConfig, get_adapter
from app.config import settings
from app.models import ApiKey, DatabaseConnection, key_database_permissions
from app.security import (
    api_key_prefix,
    encrypt_secret,
    generate_api_key,
    hash_api_key,
)
from app.utils import utcnow

MAX_TIMEOUT = 600


class ServiceError(ValueError):
    pass


def _clean_optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _parse_port(port: int | str | None) -> int | None:
    if port is None or port == "":
        return None
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ServiceError("端口必须是数字") from exc
    if not 1 <= value <= 65535:
        raise ServiceError("端口必须在 1-65535 之间")
    return value


def _normalize_options(options_text: str | None) -> str:
    text = (options_text or "").strip()
    if not text:
        return "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ServiceError(f"高级选项不是合法 JSON：{exc.msg}") from exc
    if not isinstance(value, dict):
        raise ServiceError("高级选项必须是 JSON 对象")
    return json.dumps(value, ensure_ascii=False)


def _validate_common(
    *,
    name: str,
    db_type: str,
    database: str,
    max_rows: int | str,
    query_timeout: int | str,
) -> tuple[str, str, int, int]:
    name = (name or "").strip()
    database = (database or "").strip()
    if not name:
        raise ServiceError("名称不能为空")
    if len(name) > 100:
        raise ServiceError("名称不能超过 100 个字符")
    if db_type not in DB_TYPE_INFO:
        raise ServiceError(f"不支持的数据库类型：{db_type}")
    if not database:
        raise ServiceError("数据库名/文件路径不能为空")
    try:
        rows = int(max_rows)
        timeout = int(query_timeout)
    except (TypeError, ValueError) as exc:
        raise ServiceError("最大行数和查询超时必须是整数") from exc
    rows = max(1, min(rows, settings.hard_max_rows))
    timeout = max(1, min(timeout, MAX_TIMEOUT))
    return name, database, rows, timeout


def create_database(
    session: Session,
    *,
    name: str,
    db_type: str,
    database: str,
    host: str | None = None,
    port: int | str | None = None,
    username: str | None = None,
    password: str | None = None,
    options_text: str | None = None,
    description: str | None = None,
    enabled: bool = True,
    max_rows: int | str = settings.default_max_rows,
    query_timeout: int | str = settings.default_query_timeout,
) -> DatabaseConnection:
    name, database, rows, timeout = _validate_common(
        name=name,
        db_type=db_type,
        database=database,
        max_rows=max_rows,
        query_timeout=query_timeout,
    )
    if session.scalar(select(DatabaseConnection).where(DatabaseConnection.name == name)):
        raise ServiceError(f"名称已存在：{name}")

    record = DatabaseConnection(
        name=name,
        db_type=db_type,
        host=_clean_optional(host),
        port=_parse_port(port),
        database=database,
        username=_clean_optional(username),
        password_encrypted=encrypt_secret(password) if password else None,
        options_json=_normalize_options(options_text),
        description=_clean_optional(description),
        enabled=enabled,
        max_rows=rows,
        query_timeout=timeout,
    )
    session.add(record)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ServiceError(f"名称已存在：{name}") from exc
    return record


def update_database(
    session: Session,
    record: DatabaseConnection,
    *,
    name: str,
    db_type: str,
    database: str,
    host: str | None = None,
    port: int | str | None = None,
    username: str | None = None,
    password: str | None = None,
    clear_password: bool = False,
    options_text: str | None = None,
    description: str | None = None,
    enabled: bool = True,
    max_rows: int | str = settings.default_max_rows,
    query_timeout: int | str = settings.default_query_timeout,
) -> DatabaseConnection:
    name, database, rows, timeout = _validate_common(
        name=name,
        db_type=db_type,
        database=database,
        max_rows=max_rows,
        query_timeout=query_timeout,
    )
    duplicate = session.scalar(
        select(DatabaseConnection).where(
            DatabaseConnection.name == name, DatabaseConnection.id != record.id
        )
    )
    if duplicate:
        raise ServiceError(f"名称已存在：{name}")

    record.name = name
    record.db_type = db_type
    record.host = _clean_optional(host)
    record.port = _parse_port(port)
    record.database = database
    record.username = _clean_optional(username)
    if clear_password:
        record.password_encrypted = None
    elif password:
        record.password_encrypted = encrypt_secret(password)
    record.options_json = _normalize_options(options_text)
    record.description = _clean_optional(description)
    record.enabled = enabled
    record.max_rows = rows
    record.query_timeout = timeout
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ServiceError(f"名称已存在：{name}") from exc
    return record


def delete_database(session: Session, record: DatabaseConnection) -> None:
    session.execute(
        key_database_permissions.delete().where(
            key_database_permissions.c.database_id == record.id
        )
    )
    session.delete(record)
    session.commit()


def test_database(record: DatabaseConnection) -> dict[str, Any]:
    try:
        config = DBConfig.from_model(record)
        return get_adapter(config).test_connection()
    except AdapterError as exc:
        return {"ok": False, "message": str(exc)}


def list_databases(session: Session) -> list[DatabaseConnection]:
    return list(
        session.scalars(
            select(DatabaseConnection).order_by(DatabaseConnection.name)
        ).all()
    )


def get_database(session: Session, database_id: int) -> DatabaseConnection | None:
    return session.get(DatabaseConnection, database_id)


# ---------------------------------------------------------------------- keys


def create_api_key(
    session: Session,
    *,
    name: str,
    database_ids: list[int],
    expires_at: datetime | None = None,
    enabled: bool = True,
) -> tuple[ApiKey, str]:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Key 名称不能为空")
    if len(name) > 100:
        raise ServiceError("Key 名称不能超过 100 个字符")

    raw_key = generate_api_key()
    record = ApiKey(
        name=name,
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        enabled=enabled,
        expires_at=expires_at,
    )
    record.databases = _databases_by_ids(session, database_ids)
    session.add(record)
    session.commit()
    return record, raw_key


def update_api_key(
    session: Session,
    record: ApiKey,
    *,
    name: str,
    database_ids: list[int],
    expires_at: datetime | None = None,
    enabled: bool = True,
) -> ApiKey:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Key 名称不能为空")
    record.name = name
    record.expires_at = expires_at
    record.enabled = enabled
    record.databases = _databases_by_ids(session, database_ids)
    session.commit()
    return record


def rotate_api_key(session: Session, record: ApiKey) -> str:
    raw_key = generate_api_key()
    record.key_prefix = api_key_prefix(raw_key)
    record.key_hash = hash_api_key(raw_key)
    session.commit()
    return raw_key


def delete_api_key(session: Session, record: ApiKey) -> None:
    session.execute(
        key_database_permissions.delete().where(
            key_database_permissions.c.api_key_id == record.id
        )
    )
    session.delete(record)
    session.commit()


def list_api_keys(session: Session) -> list[ApiKey]:
    return list(session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())).all())


def get_api_key(session: Session, key_id: int) -> ApiKey | None:
    return session.get(ApiKey, key_id)


def _databases_by_ids(session: Session, database_ids: list[int]) -> list[DatabaseConnection]:
    ids = {int(database_id) for database_id in database_ids}
    if not ids:
        return []
    return list(
        session.scalars(
            select(DatabaseConnection).where(DatabaseConnection.id.in_(ids))
        ).all()
    )


def authenticate_api_key(session: Session, raw_key: str | None) -> ApiKey | None:
    if not raw_key:
        return None
    record = session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key))
    )
    if record is None or not record.enabled or record.is_expired:
        return None
    return record


def mark_key_used(session: Session, record: ApiKey) -> None:
    now = utcnow()
    if (
        record.last_used_at is None
        or (now - record.last_used_at).total_seconds()
        >= settings.last_used_update_interval_seconds
    ):
        record.last_used_at = now
        session.commit()


def resolve_database_for_key(
    session: Session, key_id: int, database_name: str
) -> DatabaseConnection:
    key = session.get(ApiKey, key_id)
    if key is None or not key.enabled or key.is_expired:
        raise PermissionError("API Key 无效或已失效")

    database = session.scalar(
        select(DatabaseConnection)
        .join(
            key_database_permissions,
            key_database_permissions.c.database_id == DatabaseConnection.id,
        )
        .where(
            key_database_permissions.c.api_key_id == key_id,
            DatabaseConnection.name == database_name,
            DatabaseConnection.enabled.is_(True),
        )
    )
    if database is None:
        raise PermissionError(f"该 Key 无权访问数据库：{database_name}")
    return database


def list_databases_for_key(session: Session, key_id: int) -> list[DatabaseConnection]:
    return list(
        session.scalars(
            select(DatabaseConnection)
            .join(
                key_database_permissions,
                key_database_permissions.c.database_id == DatabaseConnection.id,
            )
            .where(
                key_database_permissions.c.api_key_id == key_id,
                DatabaseConnection.enabled.is_(True),
            )
            .order_by(DatabaseConnection.name)
        ).all()
    )
