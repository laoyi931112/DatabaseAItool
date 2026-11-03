from __future__ import annotations

import base64
import struct
import time
from dataclasses import dataclass
from typing import Any

import httpx
import sqlglot

from app.adapters.base import AdapterError, DBConfig, clamp_limit, safe_error
from app.config import settings
from app.services.query_guard import SqlGuardError, validate_readonly_sql

TUNNEL_DB_TYPES = ("mysql", "postgresql")

_MAGIC = 1111
_DIALECTS = {"mysql": "mysql", "postgresql": "postgres"}


@dataclass
class _ResultSet:
    errno: int
    numfields: int
    columns: list[str]
    rows: list[list[Any]]
    info: str | None = None
    error: str | None = None


class _ResponseReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, size: int) -> bytes:
        end = self._pos + size
        if end > len(self._data):
            raise AdapterError("隧道脚本响应不完整，可能不是 Navicat 的 ntunnel 脚本")
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def uint16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def uint32(self) -> int:
        return struct.unpack(">I", self.read(4))[0]

    def block(self) -> bytes | None:
        first = self.read(1)[0]
        if first == 0xFF:
            return None
        if first == 0xFE:
            return self.read(self.uint32())
        return self.read(first)


def _text(value: bytes | None) -> str | None:
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(value).decode("ascii")


def _message(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


def _literal(value: str, db_type: str) -> str:
    escaped = value.replace("'", "''")
    if db_type == "mysql":
        escaped = escaped.replace("\\", "\\\\")
    return f"'{escaped}'"


def _quote_ident(value: str, db_type: str) -> str:
    if db_type == "mysql":
        return "`" + value.replace("`", "``") + "`"
    return '"' + value.replace('"', '""') + '"'


def _pg_server_version(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw.isdigit():
        return raw
    number = int(raw)
    major = number // 10000
    if major >= 10:
        return f"{major}.{number % 100}"
    return f"{major}.{(number // 100) % 100}.{number % 100}"


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "t", "true", "yes", "y"}


def _limit_sql(sql: str, db_type: str, limit: int) -> str:
    dialect = _DIALECTS[db_type]
    try:
        inner = sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect)
    except Exception as exc:
        raise AdapterError(f"SQL 解析失败：{exc}") from exc
    return f"SELECT * FROM ({inner}) AS _aitool LIMIT {int(limit)}"


class NavicatTunnelAdapter:
    """Execute read-only SQL through Navicat's HTTP tunnel PHP script."""

    def __init__(self, config: DBConfig):
        if config.db_type not in TUNNEL_DB_TYPES:
            raise AdapterError("Navicat HTTP 隧道暂只支持 MySQL/MariaDB 与 PostgreSQL")
        if not config.tunnel_url:
            raise AdapterError("缺少 Navicat HTTP 隧道地址")
        self.config = config

    # ---------------------------------------------------------------- transport

    def _auth(self) -> httpx.BasicAuth | None:
        cfg = self.config
        if not cfg.tunnel_username and not cfg.tunnel_password:
            return None
        return httpx.BasicAuth(cfg.tunnel_username or "", cfg.tunnel_password or "")

    def _post(self, data: list[tuple[str, str]]) -> bytes:
        cfg = self.config
        timeout = httpx.Timeout(
            connect=float(settings.connect_timeout),
            read=max(5.0, float(cfg.query_timeout) + 5.0),
            write=30.0,
            pool=float(settings.connect_timeout),
        )
        try:
            with httpx.Client(
                auth=self._auth(),
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                response = client.post(cfg.tunnel_url or "", data=data)
        except httpx.HTTPError as exc:
            raise AdapterError(f"HTTP 隧道请求失败：{safe_error(exc)}") from exc

        if response.status_code != 200:
            preview = response.text[:300].strip()
            raise AdapterError(
                f"HTTP 隧道返回 {response.status_code}：{preview or '无响应内容'}"
            )
        return response.content

    def _request(self, actn: str, queries: list[str] | None = None) -> bytes:
        cfg = self.config
        data: list[tuple[str, str]] = [
            ("actn", actn),
            ("host", cfg.host or ""),
            ("port", str(cfg.port or "")),
            ("db", cfg.database or ""),
            ("login", cfg.username or ""),
            ("password", cfg.password or ""),
        ]
        if queries is not None:
            data.append(("encodeBase64", "1" if cfg.tunnel_encode_base64 else "0"))
            if cfg.tunnel_encode_base64:
                data.extend(
                    ("q[]", base64.b64encode(query.encode("utf-8")).decode("ascii"))
                    for query in queries
                )
            else:
                data.extend(("q[]", query) for query in queries)
        return self._post(data)

    # ------------------------------------------------------------------ framing

    @staticmethod
    def _header(reader: _ResponseReader) -> tuple[int, int]:
        magic = reader.uint32()
        version = reader.uint16()
        errno = reader.uint32()
        reader.read(6)
        if magic != _MAGIC:
            raise AdapterError(
                "隧道脚本响应格式不正确；请确认地址指向 ntunnel_mysql.php / ntunnel_pgsql.php"
            )
        return version, errno

    @staticmethod
    def _header_error(reader: _ResponseReader, errno: int) -> AdapterError:
        message = _message(reader.block())
        if message:
            return AdapterError(f"隧道脚本返回错误 {errno}：{message}")
        return AdapterError(f"隧道脚本返回错误 {errno}")

    def _connect_info(self) -> dict[str, str]:
        reader = _ResponseReader(self._request("C"))
        _version, errno = self._header(reader)
        if errno:
            raise self._header_error(reader, errno)

        blocks = [reader.block() for _ in range(3)]
        if any(block is None for block in blocks):
            raise AdapterError("隧道脚本返回的连接信息不完整")
        host_info, protocol, raw_version = (
            _text(block) or "" for block in blocks
        )
        server_version = raw_version
        if self.config.db_type == "postgresql":
            server_version = _pg_server_version(raw_version)
        return {
            "host_info": host_info,
            "protocol": protocol,
            "server_version": server_version,
        }

    def _execute(self, statements: list[str]) -> list[_ResultSet]:
        if not statements:
            return []
        if any(not statement.strip() for statement in statements):
            raise AdapterError("内部错误：隧道查询不能包含空语句")

        reader = _ResponseReader(self._request("Q", statements))
        _version, errno = self._header(reader)
        if errno:
            raise self._header_error(reader, errno)

        results: list[_ResultSet] = []
        for _ in statements:
            result_errno = reader.uint32()
            reader.uint32()  # affected rows
            reader.uint32()  # insert id
            numfields = reader.uint32()
            numrows = reader.uint32()
            reader.read(12)  # reserved

            columns: list[str] = []
            rows: list[list[Any]] = []
            info: str | None = None
            error: str | None = None

            if result_errno:
                error = _message(reader.block()) or f"错误码 {result_errno}"
            elif numfields:
                for _ in range(numfields):
                    columns.append(_text(reader.block()) or "")
                    reader.block()  # source table
                    reader.uint32()  # type
                    reader.uint32()  # flags
                    reader.uint32()  # length
                for _ in range(numrows):
                    rows.append(
                        [_text(reader.block()) for _ in range(numfields)]
                    )
            else:
                info = _message(reader.block())

            reader.read(1)  # continuation marker: 0x01 = more, 0x00 = last

            results.append(
                _ResultSet(
                    errno=result_errno,
                    numfields=numfields,
                    columns=columns,
                    rows=rows,
                    info=info,
                    error=error,
                )
            )
            if result_errno:
                raise AdapterError(f"数据库执行失败：{error}")

        return results

    def _readonly_prefix(self) -> list[str]:
        cfg = self.config
        if cfg.db_type == "mysql":
            return ["SET SESSION TRANSACTION READ ONLY"]
        timeout_ms = max(1, cfg.query_timeout) * 1000
        return [
            "SET default_transaction_read_only = on",
            f"SET statement_timeout = {timeout_ms}",
        ]

    def _execute_readonly(self, statements: list[str]) -> list[_ResultSet]:
        prefix = self._readonly_prefix()
        results = self._execute([*prefix, *statements])
        return results[len(prefix) :]

    # ----------------------------------------------------------------- metadata

    def _resolve_schema(self, schema: str | None) -> str:
        cfg = self.config
        if cfg.db_type == "mysql":
            if schema and schema.lower() != cfg.database.lower():
                raise AdapterError(
                    f"该连接只允许访问数据库 {cfg.database}，不能访问 {schema}"
                )
            return cfg.database
        return schema or "public"

    def _table_info(self, schema: str, table: str) -> tuple[str, str]:
        cfg = self.config
        sql = (
            "SELECT table_name, table_type FROM information_schema.tables "
            f"WHERE table_schema = {_literal(schema, cfg.db_type)} "
            f"AND LOWER(table_name) = LOWER({_literal(table, cfg.db_type)})"
        )
        results = self._execute_readonly([sql])
        rows = results[0].rows
        if not rows:
            raise AdapterError(f"表或视图不存在：{table}")
        name = str(rows[0][0])
        kind = "table" if str(rows[0][1]).upper() == "BASE TABLE" else "view"
        return name, kind

    def _current_schema(self) -> str:
        try:
            results = self._execute_readonly(["SELECT current_schema()"])
            if results[0].rows and results[0].rows[0]:
                return str(results[0].rows[0][0] or "public")
        except AdapterError:
            pass
        return "public"

    def get_database_info(self) -> dict[str, Any]:
        cfg = self.config
        try:
            info = self._connect_info()
            default_schema = cfg.database
            if cfg.db_type == "postgresql":
                default_schema = self._current_schema()
            return {
                "name": cfg.name,
                "type": cfg.db_type,
                "database": cfg.database,
                "server_version": info["server_version"],
                "default_schema": default_schema,
                "read_only": True,
                "transport": "navicat_http_tunnel",
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取数据库信息失败：{safe_error(exc)}") from exc

    def list_schemas(self) -> list[dict[str, Any]]:
        cfg = self.config
        if cfg.db_type == "mysql":
            return [{"name": cfg.database}]
        try:
            results = self._execute_readonly(
                [
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') "
                    "AND schema_name NOT LIKE 'pg_%' ORDER BY schema_name"
                ]
            )
            return [{"name": str(row[0])} for row in results[0].rows]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取 schema 失败：{safe_error(exc)}") from exc

    def list_tables(self, schema: str | None = None) -> list[dict[str, Any]]:
        cfg = self.config
        schema_name = self._resolve_schema(schema)
        sql = (
            "SELECT table_name, table_type FROM information_schema.tables "
            f"WHERE table_schema = {_literal(schema_name, cfg.db_type)} "
            "ORDER BY table_name"
        )
        try:
            results = self._execute_readonly([sql])
            items = [
                {
                    "name": str(row[0]),
                    "type": "table" if str(row[1]).upper() == "BASE TABLE" else "view",
                }
                for row in results[0].rows
            ]
            return sorted(items, key=lambda item: (item["type"], item["name"]))
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取表列表失败：{safe_error(exc)}") from exc

    def _describe_mysql(self, schema: str, table: str) -> dict[str, Any]:
        actual, kind = self._table_info(schema, table)
        schema_lit = _literal(schema, "mysql")
        table_lit = _literal(actual, "mysql")
        column_sql = (
            "SELECT column_name, column_type, is_nullable, column_default, "
            "extra, column_comment FROM information_schema.columns "
            f"WHERE table_schema = {schema_lit} AND table_name = {table_lit} "
            "ORDER BY ordinal_position"
        )
        pk_sql = (
            "SELECT column_name FROM information_schema.key_column_usage "
            f"WHERE table_schema = {schema_lit} AND table_name = {table_lit} "
            "AND constraint_name = 'PRIMARY' ORDER BY ordinal_position"
        )
        fk_sql = (
            "SELECT constraint_name, column_name, referenced_table_name, "
            "referenced_column_name FROM information_schema.key_column_usage "
            f"WHERE table_schema = {schema_lit} AND table_name = {table_lit} "
            "AND referenced_table_name IS NOT NULL "
            "ORDER BY constraint_name, ordinal_position"
        )
        index_sql = (
            "SELECT index_name, column_name, non_unique "
            "FROM information_schema.statistics "
            f"WHERE table_schema = {schema_lit} AND table_name = {table_lit} "
            "ORDER BY index_name, seq_in_index"
        )
        _columns, pk, fk, indexes = self._execute_readonly(
            [column_sql, pk_sql, fk_sql, index_sql]
        )

        columns = [
            {
                "name": str(row[0]),
                "type": str(row[1]),
                "nullable": str(row[2]).upper() == "YES",
                "default": row[3],
                "autoincrement": "auto_increment" in str(row[4] or "").lower(),
                "comment": row[5],
            }
            for row in _columns.rows
        ]
        primary_key = [str(row[0]) for row in pk.rows]

        foreign: dict[str, dict[str, Any]] = {}
        for name, column, referred_table, referred_column in fk.rows:
            entry = foreign.setdefault(
                str(name),
                {
                    "name": str(name),
                    "columns": [],
                    "referred_table": str(referred_table),
                    "referred_columns": [],
                },
            )
            entry["columns"].append(str(column))
            entry["referred_columns"].append(str(referred_column))

        grouped: dict[str, dict[str, Any]] = {}
        for name, column, non_unique in indexes.rows:
            entry = grouped.setdefault(
                str(name),
                {"name": str(name), "columns": [], "unique": str(non_unique) == "0"},
            )
            entry["columns"].append(str(column))

        return {
            "database": self.config.name,
            "schema": schema,
            "table": actual,
            "type": kind,
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": list(foreign.values()),
            "indexes": list(grouped.values()),
        }

    def _describe_postgresql(self, schema: str, table: str) -> dict[str, Any]:
        actual, kind = self._table_info(schema, table)
        schema_lit = _literal(schema, "postgresql")
        table_lit = _literal(actual, "postgresql")
        column_sql = (
            "SELECT column_name, data_type, udt_name, is_nullable, column_default, "
            "character_maximum_length, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            f"WHERE table_schema = {schema_lit} AND table_name = {table_lit} "
            "ORDER BY ordinal_position"
        )
        pk_sql = (
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"AND tc.table_schema = {schema_lit} AND tc.table_name = {table_lit} "
            "ORDER BY kcu.ordinal_position"
        )
        fk_sql = (
            "SELECT c.conname, a.attname, ft.relname, fa.attname "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_class ft ON ft.oid = c.confrelid "
            "JOIN pg_namespace fn ON fn.oid = ft.relnamespace "
            "JOIN LATERAL unnest(c.conkey, c.confkey) "
            "WITH ORDINALITY AS k(attnum, fattnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "JOIN pg_attribute fa ON fa.attrelid = ft.oid AND fa.attnum = k.fattnum "
            "WHERE c.contype = 'f' "
            f"AND n.nspname = {schema_lit} AND t.relname = {table_lit} "
            "ORDER BY c.conname, k.ord"
        )
        index_sql = (
            "SELECT i.relname, a.attname, ix.indisunique "
            "FROM pg_class t "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_index ix ON ix.indrelid = t.oid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_attribute a ON a.attrelid = t.oid "
            "AND a.attnum = ANY(ix.indkey) "
            f"WHERE n.nspname = {schema_lit} AND t.relname = {table_lit} "
            "ORDER BY i.relname, a.attnum"
        )
        _columns, _pk, fk, indexes = self._execute_readonly(
            [column_sql, pk_sql, fk_sql, index_sql]
        )

        columns = []
        for row in _columns.rows:
            data_type = str(row[1])
            if data_type.upper() == "USER-DEFINED" and row[2]:
                data_type = str(row[2])
            length, precision, scale = row[5], row[6], row[7]
            if length is not None:
                data_type = f"{data_type}({length})"
            elif precision is not None and scale is not None and data_type.lower() in {
                "numeric",
                "decimal",
            }:
                data_type = f"{data_type}({precision},{scale})"
            columns.append(
                {
                    "name": str(row[0]),
                    "type": data_type,
                    "nullable": str(row[3]).upper() == "YES",
                    "default": row[4],
                    "autoincrement": "nextval(" in str(row[4] or "").lower(),
                    "comment": None,
                }
            )

        primary_key = [str(row[0]) for row in _pk.rows]

        foreign: dict[str, dict[str, Any]] = {}
        for name, column, referred_table, referred_column in fk.rows:
            entry = foreign.setdefault(
                str(name),
                {
                    "name": str(name),
                    "columns": [],
                    "referred_table": str(referred_table),
                    "referred_columns": [],
                },
            )
            entry["columns"].append(str(column))
            entry["referred_columns"].append(str(referred_column))

        grouped: dict[str, dict[str, Any]] = {}
        for name, column, unique in indexes.rows:
            entry = grouped.setdefault(
                str(name),
                {"name": str(name), "columns": [], "unique": _as_bool(unique)},
            )
            entry["columns"].append(str(column))

        return {
            "database": self.config.name,
            "schema": schema,
            "table": actual,
            "type": kind,
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": list(foreign.values()),
            "indexes": list(grouped.values()),
        }

    def describe_table(
        self, table: str, schema: str | None = None
    ) -> dict[str, Any]:
        schema_name = self._resolve_schema(schema)
        try:
            if self.config.db_type == "mysql":
                return self._describe_mysql(schema_name, table)
            return self._describe_postgresql(schema_name, table)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取表结构失败：{safe_error(exc)}") from exc

    # ------------------------------------------------------------------- query

    def test_connection(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            info = self._connect_info()
            latency = round((time.perf_counter() - started) * 1000, 1)
            return {
                "ok": True,
                "message": "通过 HTTP 隧道连接成功（只读）",
                "server_version": info["server_version"],
                "latency_ms": latency,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"连接失败：{safe_error(exc)}") from exc

    def query(self, sql: str, max_rows: int | None = None) -> dict[str, Any]:
        cfg = self.config
        try:
            safe_sql = validate_readonly_sql(sql, cfg.db_type, cfg.database)
        except SqlGuardError as exc:
            raise AdapterError(str(exc)) from exc

        limit = clamp_limit(cfg, max_rows)
        limited_sql = _limit_sql(safe_sql, cfg.db_type, limit + 1)
        try:
            results = self._execute_readonly([limited_sql])
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"查询失败：{safe_error(exc)}") from exc

        result = results[0]
        rows = result.rows
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "database": cfg.name,
            "columns": result.columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": limit,
        }

    def sample_rows(
        self, table: str, schema: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        schema_name = self._resolve_schema(schema)
        try:
            actual, _kind = self._table_info(schema_name, table)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取表数据失败：{safe_error(exc)}") from exc

        cfg = self.config
        qualified = _quote_ident(actual, cfg.db_type)
        if cfg.db_type == "postgresql":
            qualified = f"{_quote_ident(schema_name, cfg.db_type)}.{qualified}"
        return self.query(f"SELECT * FROM {qualified}", max_rows=limit)
