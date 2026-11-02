from __future__ import annotations

import sqlglot
from sqlglot import exp

DIALECTS = {
    "mysql": "mysql",
    "postgresql": "postgres",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "oracle": "oracle",
}

ALLOWED_ROOTS = {"Select", "Union", "Except", "Intersect"}

FORBIDDEN_NODES = {
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Command",
    "Grant",
    "Revoke",
    "Copy",
    "Attach",
    "Detach",
    "Into",
    "Lock",
    "Transaction",
    "Commit",
    "Rollback",
    "Set",
    "Use",
    "Pragma",
    "Call",
    "Execute",
    "LoadData",
    "Export",
    "Analyze",
    "Optimize",
    "Vacuum",
    "Reindex",
    "Refresh",
}

# 这些函数可能读写服务器文件、管理会话或制造长时间阻塞，即使是 SELECT 也不放行。
FORBIDDEN_FUNCTIONS = {
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_switch_wal",
    "pg_promote",
    "pg_create_restore_point",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "lo_import",
    "lo_export",
    "nextval",
    "setval",
    "dblink",
    "dblink_exec",
    "load_file",
    "sleep",
    "benchmark",
    "xp_cmdshell",
    "sp_executesql",
    "openrowset",
    "openquery",
    "opendatasource",
    "bulkadmin",
}
FORBIDDEN_FUNCTION_PREFIXES = ("dbms_", "utl_", "ctxsys.")


class SqlGuardError(ValueError):
    pass


def validate_readonly_sql(sql: str, db_type: str, database: str | None = None) -> str:
    """Parse and validate a SQL statement without rewriting or executing it."""
    sql = (sql or "").strip()
    if not sql:
        raise SqlGuardError("SQL 不能为空")

    # MySQL 会执行 /*! ... */ 中的内容，不能让解析器忽略它。
    if db_type == "mysql" and "/*!" in sql:
        raise SqlGuardError("不允许使用 MySQL 可执行注释 /*! ... */")

    dialect = DIALECTS.get(db_type, "mysql")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as exc:  # sqlglot raises various parse errors
        raise SqlGuardError(f"SQL 解析失败：{exc}") from exc

    if len(statements) != 1:
        raise SqlGuardError("只允许执行单条 SQL 语句")

    root = statements[0]
    root_name = type(root).__name__
    if root_name not in ALLOWED_ROOTS:
        raise SqlGuardError(f"只允许 SELECT 查询，检测到 {root_name}")

    for node in root.walk():
        node_name = type(node).__name__
        if node_name in FORBIDDEN_NODES:
            raise SqlGuardError(f"不允许的 SQL 操作：{node_name}")

    _check_functions(root)
    _check_table_scope(root, db_type, database)
    return sql


def _check_functions(root: exp.Expression) -> None:
    for func in root.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            raw_name = str(func.this)
        else:
            raw_name = func.sql_name()
        name = (raw_name or "").lower()
        if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            raise SqlGuardError(f"不允许调用函数：{raw_name}")


def _check_table_scope(
    root: exp.Expression, db_type: str, database: str | None
) -> None:
    if not database:
        return
    configured = database.lower()
    for table in root.find_all(exp.Table):
        if db_type == "mysql":
            if table.db and table.db.lower() != configured:
                raise SqlGuardError(
                    f"该 Key 只允许访问数据库 {database}，不能查询 {table.db}"
                )
        elif db_type == "mssql":
            catalog = getattr(table, "catalog", "") or ""
            if catalog and catalog.lower() != configured:
                raise SqlGuardError(
                    f"该 Key 只允许访问数据库 {database}，不能查询 {catalog}"
                )
