from __future__ import annotations

from typing import Any

from app.adapters import DBConfig, get_adapter
from app.adapters.base import AdapterError
from app.adapters.mongo import MongoAdapter
from app.db import SessionLocal
from app.models import ApiKey
from app.services import connections as connection_service


def _ensure_key(session, key_id: int) -> ApiKey:
    key = session.get(ApiKey, key_id)
    if key is None or not key.enabled or key.is_expired:
        raise PermissionError("API Key 无效、已禁用或已过期")
    return key


def _config_for(key_id: int, database_name: str) -> DBConfig:
    with SessionLocal() as session:
        record = connection_service.resolve_database_for_key(
            session, key_id, database_name
        )
        return DBConfig.from_model(record)


def list_databases(key_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        _ensure_key(session, key_id)
        records = connection_service.list_databases_for_key(session, key_id)
        return {
            "databases": [
                {
                    "name": record.name,
                    "type": record.db_type,
                    "description": record.description,
                    "max_rows": record.max_rows,
                }
                for record in records
            ]
        }


def get_database_info(key_id: int, database: str) -> dict[str, Any]:
    return get_adapter(_config_for(key_id, database)).get_database_info()


def list_schemas(key_id: int, database: str) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    return {"database": database, "schemas": adapter.list_schemas()}


def list_tables(
    key_id: int, database: str, schema: str | None = None
) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    return {"database": database, "schema": schema, "tables": adapter.list_tables(schema)}


def describe_table(
    key_id: int, database: str, table: str, schema: str | None = None
) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    return adapter.describe_table(table, schema)


def query(
    key_id: int, database: str, sql: str, max_rows: int | None = None
) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    return adapter.query(sql, max_rows)


def sample_rows(
    key_id: int,
    database: str,
    table: str,
    schema: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    return adapter.sample_rows(table, schema, limit)


def find_documents(
    key_id: int,
    database: str,
    collection: str,
    filter: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
    sort: dict[str, Any] | list[Any] | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    if not isinstance(adapter, MongoAdapter):
        raise AdapterError("find_documents 仅适用于 MongoDB 数据库")
    return adapter.find_documents(collection, filter, projection, sort, limit)


def aggregate_documents(
    key_id: int,
    database: str,
    collection: str,
    pipeline: list[dict[str, Any]],
    limit: int | None = 50,
) -> dict[str, Any]:
    adapter = get_adapter(_config_for(key_id, database))
    if not isinstance(adapter, MongoAdapter):
        raise AdapterError("aggregate_documents 仅适用于 MongoDB 数据库")
    return adapter.aggregate_documents(collection, pipeline, limit)
