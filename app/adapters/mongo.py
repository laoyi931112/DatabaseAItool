from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from bson import Decimal128, ObjectId
from pymongo import MongoClient
from pymongo.database import Database

from app.adapters.base import AdapterError, DBConfig, clamp_limit, jsonable, safe_error

MONGO_OPTION_KEYS = {
    "authSource",
    "authMechanism",
    "tls",
    "tlsAllowInvalidCertificates",
    "tlsCAFile",
    "tlsCertificateKeyFile",
    "replicaSet",
    "directConnection",
    "readPreference",
    "retryWrites",
}


def _mongo_jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return jsonable(value)


def _bson_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, datetime):
        return "date"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, Decimal128):
        return "decimal"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, (bytes, bytearray)):
        return "binData"
    return type(value).__name__


class MongoAdapter:
    def __init__(self, config: DBConfig):
        self.config = config

    @contextmanager
    def _database(self) -> Iterator[Database]:
        cfg = self.config
        options = cfg.options
        try:
            timeout_ms = max(1, cfg.query_timeout) * 1000
            common = {
                "serverSelectionTimeoutMS": 5000,
                "connectTimeoutMS": 5000,
                "socketTimeoutMS": timeout_ms,
                "appname": "DatabaseAItool",
            }
            if options.get("uri"):
                client = MongoClient(str(options["uri"]), **common)
            else:
                kwargs: dict[str, Any] = dict(common)
                if cfg.username:
                    kwargs["username"] = cfg.username
                    kwargs["password"] = cfg.password or ""
                    kwargs.setdefault(
                        "authSource", str(options.get("authSource") or cfg.database)
                    )
                for key in MONGO_OPTION_KEYS:
                    if key in options:
                        kwargs[key] = options[key]
                client = MongoClient(
                    host=cfg.host or "127.0.0.1",
                    port=cfg.port or 27017,
                    **kwargs,
                )
            try:
                yield client[cfg.database]
            finally:
                client.close()
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"MongoDB 连接失败：{safe_error(exc)}") from exc

    def _collection_names(self, database: Database) -> list[str]:
        return list(database.list_collection_names())

    def _require_collection(self, database: Database, collection: str) -> None:
        if collection not in self._collection_names(database):
            raise AdapterError(f"集合不存在：{collection}")

    def test_connection(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with self._database() as database:
                database.client.admin.command("ping")
                try:
                    version = database.client.server_info().get("version")
                except Exception:
                    version = None
            latency = round((time.perf_counter() - started) * 1000, 1)
            return {
                "ok": True,
                "message": "连接成功（只读操作）",
                "server_version": version,
                "latency_ms": latency,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"连接失败：{safe_error(exc)}") from exc

    def get_database_info(self) -> dict[str, Any]:
        cfg = self.config
        try:
            with self._database() as database:
                try:
                    version = database.client.server_info().get("version")
                except Exception:
                    version = None
                collections = self._collection_names(database)
            return {
                "name": cfg.name,
                "type": "mongodb",
                "database": cfg.database,
                "server_version": version,
                "default_schema": cfg.database,
                "collection_count": len(collections),
                "read_only": True,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取数据库信息失败：{safe_error(exc)}") from exc

    def list_schemas(self) -> list[dict[str, Any]]:
        return [{"name": self.config.database}]

    def list_tables(self, schema: str | None = None) -> list[dict[str, Any]]:
        if schema and schema != self.config.database:
            raise AdapterError(f"该连接只允许访问数据库 {self.config.database}")
        try:
            with self._database() as database:
                collections = database.list_collections()
                items = [
                    {
                        "name": info["name"],
                        "type": "view" if info.get("type") == "view" else "collection",
                    }
                    for info in collections
                ]
            return sorted(items, key=lambda item: item["name"])
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取集合列表失败：{safe_error(exc)}") from exc

    def describe_table(
        self, table: str, schema: str | None = None
    ) -> dict[str, Any]:
        if schema and schema != self.config.database:
            raise AdapterError(f"该连接只允许访问数据库 {self.config.database}")
        try:
            with self._database() as database:
                self._require_collection(database, table)
                collection = database[table]
                sample = list(collection.find({}).limit(50))

                fields: dict[str, dict[str, Any]] = {}
                for document in sample:
                    for key, value in document.items():
                        entry = fields.setdefault(
                            key, {"name": key, "types": set(), "occurrences": 0}
                        )
                        entry["types"].add(_bson_type(value))
                        entry["occurrences"] += 1

                columns = [
                    {
                        "name": entry["name"],
                        "types": sorted(entry["types"]),
                        "occurrences": entry["occurrences"],
                        "nullable": entry["occurrences"] < len(sample),
                    }
                    for entry in fields.values()
                ]

                indexes = [
                    {
                        "name": name,
                        "columns": sorted(list(spec.get("key", {}).keys())),
                        "unique": spec.get("unique", False),
                    }
                    for name, spec in collection.index_information().items()
                ]
            return {
                "database": self.config.name,
                "schema": self.config.database,
                "table": table,
                "type": "collection",
                "columns": columns,
                "primary_key": ["_id"],
                "foreign_keys": [],
                "indexes": indexes,
                "sample_size": len(sample),
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取集合结构失败：{safe_error(exc)}") from exc

    def sample_rows(
        self, table: str, schema: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        return self.find_documents(table, limit=limit)

    def query(self, sql: str, max_rows: int | None = None) -> dict[str, Any]:
        raise AdapterError(
            "MongoDB 不支持 SQL 查询，请使用 find_documents 或 aggregate_documents"
        )

    def find_documents(
        self,
        collection: str,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        sort: dict[str, Any] | list[Any] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if filter is not None and not isinstance(filter, dict):
            raise AdapterError("filter 必须是 JSON 对象")
        if projection is not None and not isinstance(projection, dict):
            raise AdapterError("projection 必须是 JSON 对象")
        if sort is not None and not isinstance(sort, (dict, list)):
            raise AdapterError("sort 必须是 JSON 对象或数组")

        effective = clamp_limit(self.config, limit)
        try:
            with self._database() as database:
                self._require_collection(database, collection)
                cursor = database[collection].find(filter or {}, projection)
                if sort:
                    cursor = cursor.sort(
                        list(sort.items()) if isinstance(sort, dict) else sort
                    )
                documents = list(cursor.limit(effective + 1))
            truncated = len(documents) > effective
            documents = documents[:effective]
            return {
                "database": self.config.name,
                "collection": collection,
                "documents": [_mongo_jsonable(document) for document in documents],
                "row_count": len(documents),
                "truncated": truncated,
                "max_rows": effective,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"查询文档失败：{safe_error(exc)}") from exc

    def aggregate_documents(
        self,
        collection: str,
        pipeline: list[dict[str, Any]],
        limit: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(pipeline, list) or not all(
            isinstance(stage, dict) for stage in pipeline
        ):
            raise AdapterError("pipeline 必须是 JSON 对象数组")
        for stage in pipeline:
            if any(key in ("$out", "$merge") for key in stage):
                raise AdapterError("聚合管道中不允许使用 $out 或 $merge（会写入数据）")

        effective = clamp_limit(self.config, limit)
        try:
            with self._database() as database:
                self._require_collection(database, collection)
                documents = list(
                    database[collection].aggregate(
                        [*pipeline, {"$limit": effective + 1}]
                    )
                )
            truncated = len(documents) > effective
            documents = documents[:effective]
            return {
                "database": self.config.name,
                "collection": collection,
                "documents": [_mongo_jsonable(document) for document in documents],
                "row_count": len(documents),
                "truncated": truncated,
                "max_rows": effective,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"聚合查询失败：{safe_error(exc)}") from exc
