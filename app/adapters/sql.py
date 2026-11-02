from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.pool import NullPool

from app.adapters.base import AdapterError, DBConfig, clamp_limit, jsonable, safe_error
from app.config import BASE_DIR, settings
from app.services.query_guard import SqlGuardError, validate_readonly_sql

SQL_DRIVERS = {
    "mysql": "mysql+pymysql",
    "postgresql": "postgresql+psycopg",
    "mssql": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
}

VERSION_QUERIES = {
    "mysql": "SELECT VERSION()",
    "postgresql": "SELECT version()",
    "sqlite": "SELECT sqlite_version()",
    "mssql": "SELECT @@VERSION",
}


class SQLAdapter:
    def __init__(self, config: DBConfig):
        if config.db_type not in (*SQL_DRIVERS, "sqlite"):
            raise AdapterError(f"不支持的数据库类型：{config.db_type}")
        self.config = config

    # ------------------------------------------------------------------ engine

    def _create_engine(self) -> Engine:
        cfg = self.config
        try:
            if cfg.db_type == "sqlite":
                return self._create_sqlite_engine()

            options = cfg.options
            query = {
                str(key): str(value) for key, value in (options.get("query") or {}).items()
            }
            connect_args = dict(options.get("connect_args") or {})
            timeout_ms = max(1, cfg.query_timeout) * 1000

            if cfg.db_type == "mysql":
                connect_args.setdefault("connect_timeout", settings.connect_timeout)
                connect_args.setdefault("read_timeout", cfg.query_timeout)
                connect_args.setdefault("write_timeout", cfg.query_timeout)
                if options.get("charset"):
                    connect_args.setdefault("charset", str(options["charset"]))
                if "ssl" in options:
                    connect_args.setdefault("ssl", options["ssl"])
                if options.get("ssl_disabled") is True:
                    connect_args.setdefault("ssl_disabled", True)
            elif cfg.db_type == "postgresql":
                connect_args.setdefault("connect_timeout", settings.connect_timeout)
                connect_args.setdefault(
                    "options", f"-c statement_timeout={timeout_ms}"
                )
            elif cfg.db_type == "mssql":
                connect_args.setdefault("timeout", settings.connect_timeout)
                query.setdefault(
                    "driver",
                    str(options.get("driver") or "ODBC Driver 18 for SQL Server"),
                )
                if options.get("trust_server_certificate") is True:
                    query.setdefault("TrustServerCertificate", "yes")
            elif cfg.db_type == "oracle":
                connect_args.setdefault("tcp_connect_timeout", settings.connect_timeout)

            url = URL.create(
                SQL_DRIVERS[cfg.db_type],
                username=cfg.username or None,
                password=cfg.password or None,
                host=cfg.host or None,
                port=cfg.port,
                database=cfg.database,
                query=query,
            )
            engine = create_engine(
                url,
                connect_args=connect_args,
                pool_recycle=1800,
            )
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"创建连接失败：{safe_error(exc)}") from exc

        self._register_engine_events(engine)
        return engine

    def _create_sqlite_engine(self) -> Engine:
        path = Path(self.config.database).expanduser()
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        if not path.is_file():
            raise AdapterError(f"SQLite 文件不存在：{path}")

        uri = path.as_uri() + "?mode=ro"
        timeout = max(1, self.config.query_timeout)

        def creator() -> sqlite3.Connection:
            connection = sqlite3.connect(uri, uri=True, timeout=timeout)
            deadline = time.monotonic() + timeout
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 10_000
            )
            return connection

        engine = create_engine("sqlite://", creator=creator, poolclass=NullPool)
        self._register_engine_events(engine)
        return engine

    def _register_engine_events(self, engine: Engine) -> None:
        cfg = self.config
        timeout_ms = max(1, cfg.query_timeout) * 1000

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection, _record) -> None:
            self._configure_connection(dbapi_connection, timeout_ms)

        @event.listens_for(engine, "before_cursor_execute")
        def _on_before_execute(
            _conn, cursor, _statement, _parameters, _context, _executemany
        ) -> None:
            if cfg.db_type == "mssql":
                try:
                    cursor.timeout = cfg.query_timeout
                except Exception:
                    pass

    def _configure_connection(self, dbapi_connection, timeout_ms: int) -> None:
        cfg = self.config
        if cfg.db_type == "sqlite":
            return

        cursor = dbapi_connection.cursor()
        try:
            if cfg.db_type == "postgresql":
                cursor.execute("SET default_transaction_read_only = on")
            elif cfg.db_type == "mysql":
                last_error: Exception | None = None
                for statement in (
                    "SET SESSION TRANSACTION READ ONLY",
                    "SET SESSION tx_read_only = 1",
                ):
                    try:
                        cursor.execute(statement)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                if last_error is not None:
                    raise AdapterError(
                        "无法将会话设置为只读，请确认 MySQL 版本 >= 5.7 / MariaDB >= 10.0"
                    ) from last_error
                try:
                    cursor.execute(f"SET SESSION max_execution_time = {timeout_ms}")
                except Exception:
                    pass
            elif cfg.db_type == "oracle":
                try:
                    dbapi_connection.call_timeout = timeout_ms
                except Exception:
                    pass
        finally:
            cursor.close()

    @contextmanager
    def _engine(self) -> Iterator[Engine]:
        engine = self._create_engine()
        try:
            yield engine
        finally:
            engine.dispose()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _server_version(connection) -> str | None:
        dialect_name = connection.dialect.name
        statement = VERSION_QUERIES.get(dialect_name)
        if statement:
            try:
                value = connection.exec_driver_sql(statement).scalar()
                if value:
                    return str(value).strip()
            except Exception:
                pass
        version_info = connection.dialect.server_version_info
        if version_info:
            return ".".join(str(part) for part in version_info)
        return None

    def _resolve_schema(self, schema: str | None) -> str | None:
        cfg = self.config
        if cfg.db_type == "mysql":
            if schema and schema.lower() != cfg.database.lower():
                raise AdapterError(
                    f"该连接只允许访问数据库 {cfg.database}，不能访问 {schema}"
                )
            return cfg.database
        if cfg.db_type == "sqlite":
            return schema or None
        return schema or None

    def _find_table(self, inspector, schema: str | None, table: str) -> tuple[str, str]:
        try:
            tables = inspector.get_table_names(schema=schema)
        except Exception:
            tables = []
        try:
            views = inspector.get_view_names(schema=schema)
        except Exception:
            views = []

        for name, kind in [(name, "table") for name in tables] + [
            (name, "view") for name in views
        ]:
            if name == table:
                return name, kind

        lowered = table.lower()
        for name, kind in [(name, "table") for name in tables] + [
            (name, "view") for name in views
        ]:
            if name.lower() == lowered:
                return name, kind

        raise AdapterError(f"表或视图不存在：{table}")

    def _prepare_transaction(self, connection) -> None:
        if self.config.db_type == "oracle":
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    # -------------------------------------------------------------------- read

    def test_connection(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with self._engine() as engine:
                with engine.connect() as connection:
                    connection.exec_driver_sql("SELECT 1").scalar()
                    version = self._server_version(connection)
            latency = round((time.perf_counter() - started) * 1000, 1)
            return {
                "ok": True,
                "message": "连接成功（只读）",
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
            with self._engine() as engine:
                with engine.connect() as connection:
                    version = self._server_version(connection)
                    default_schema = connection.dialect.default_schema_name
            if cfg.db_type == "sqlite":
                default_schema = "main"
            return {
                "name": cfg.name,
                "type": cfg.db_type,
                "database": cfg.database,
                "server_version": version,
                "default_schema": default_schema,
                "read_only": True,
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
            with self._engine() as engine:
                inspector = inspect(engine)
                names = inspector.get_schema_names()
            return [{"name": name} for name in names]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取 schema 失败：{safe_error(exc)}") from exc

    def list_tables(self, schema: str | None = None) -> list[dict[str, Any]]:
        schema_name = self._resolve_schema(schema)
        try:
            with self._engine() as engine:
                inspector = inspect(engine)
                try:
                    tables = inspector.get_table_names(schema=schema_name)
                except Exception:
                    tables = []
                try:
                    views = inspector.get_view_names(schema=schema_name)
                except Exception:
                    views = []
            result = [{"name": name, "type": "table"} for name in tables]
            result += [{"name": name, "type": "view"} for name in views]
            return sorted(result, key=lambda item: (item["type"], item["name"]))
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取表列表失败：{safe_error(exc)}") from exc

    def describe_table(
        self, table: str, schema: str | None = None
    ) -> dict[str, Any]:
        schema_name = self._resolve_schema(schema)
        try:
            with self._engine() as engine:
                inspector = inspect(engine)
                actual, kind = self._find_table(inspector, schema_name, table)

                columns = [
                    {
                        "name": column.get("name"),
                        "type": str(column.get("type")),
                        "nullable": column.get("nullable"),
                        "default": column.get("default"),
                        "autoincrement": column.get("autoincrement"),
                        "comment": column.get("comment"),
                    }
                    for column in inspector.get_columns(actual, schema=schema_name)
                ]

                try:
                    pk = inspector.get_pk_constraint(actual, schema=schema_name)
                    primary_key = pk.get("constrained_columns") or []
                except Exception:
                    primary_key = []

                try:
                    foreign_keys = [
                        {
                            "name": fk.get("name"),
                            "columns": fk.get("constrained_columns"),
                            "referred_table": fk.get("referred_table"),
                            "referred_columns": fk.get("referred_columns"),
                        }
                        for fk in inspector.get_foreign_keys(actual, schema=schema_name)
                    ]
                except Exception:
                    foreign_keys = []

                try:
                    indexes = [
                        {
                            "name": index.get("name"),
                            "columns": index.get("column_names"),
                            "unique": index.get("unique"),
                        }
                        for index in inspector.get_indexes(actual, schema=schema_name)
                    ]
                except Exception:
                    indexes = []

            return {
                "database": self.config.name,
                "schema": schema_name,
                "table": actual,
                "type": kind,
                "columns": columns,
                "primary_key": primary_key,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取表结构失败：{safe_error(exc)}") from exc

    def query(self, sql: str, max_rows: int | None = None) -> dict[str, Any]:
        cfg = self.config
        try:
            safe_sql = validate_readonly_sql(sql, cfg.db_type, cfg.database)
        except SqlGuardError as exc:
            raise AdapterError(str(exc)) from exc

        limit = clamp_limit(cfg, max_rows)
        try:
            with self._engine() as engine:
                with engine.connect() as connection:
                    self._prepare_transaction(connection)
                    result = connection.execute(text(safe_sql))
                    columns = list(result.keys())
                    fetched = result.fetchmany(limit + 1)
                    truncated = len(fetched) > limit
                    rows = [[jsonable(value) for value in row] for row in fetched[:limit]]
            return {
                "database": cfg.name,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "max_rows": limit,
            }
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"查询失败：{safe_error(exc)}") from exc

    def sample_rows(
        self, table: str, schema: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        schema_name = self._resolve_schema(schema)
        try:
            with self._engine() as engine:
                inspector = inspect(engine)
                actual, _kind = self._find_table(inspector, schema_name, table)
                preparer = engine.dialect.identifier_preparer
                qualified = preparer.quote(actual)
                if schema_name and self.config.db_type != "sqlite":
                    qualified = f"{preparer.quote(schema_name)}.{qualified}"
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"读取表数据失败：{safe_error(exc)}") from exc

        return self.query(f"SELECT * FROM {qualified}", max_rows=limit)
