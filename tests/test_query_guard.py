from __future__ import annotations

import pytest

from app.services.query_guard import SqlGuardError, validate_readonly_sql


@pytest.mark.parametrize(
    "sql,dialect",
    [
        ("SELECT 1", "sqlite"),
        ("SELECT * FROM users", "mysql"),
        ("WITH x AS (SELECT 1 AS a) SELECT * FROM x", "postgresql"),
        ("SELECT id FROM users UNION SELECT id FROM users", "mysql"),
        ("SELECT COUNT(*) FROM users -- comment", "postgresql"),
        ("SELECT name FROM users WHERE name = 'DROP TABLE users'", "sqlite"),
    ],
)
def test_allows_select(sql: str, dialect: str) -> None:
    assert validate_readonly_sql(sql, dialect, "app") == sql.strip()


@pytest.mark.parametrize(
    "sql,dialect",
    [
        ("INSERT INTO users VALUES (1)", "mysql"),
        ("UPDATE users SET name='x'", "postgresql"),
        ("DELETE FROM users", "sqlite"),
        ("DROP TABLE users", "mysql"),
        ("CREATE TABLE t (id INT)", "postgresql"),
        ("ALTER TABLE users ADD COLUMN x INT", "mysql"),
        ("TRUNCATE TABLE users", "mysql"),
        ("PRAGMA writable_schema = 1", "sqlite"),
        ("ATTACH DATABASE '/tmp/x.db' AS x", "sqlite"),
        ("SELECT * FROM users FOR UPDATE", "postgresql"),
        ("SELECT * INTO OUTFILE '/tmp/x' FROM users", "mysql"),
        ("SELECT 1; DROP TABLE users", "sqlite"),
        ("/*!50000 DROP TABLE users */ SELECT 1", "mysql"),
        ("WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d", "postgresql"),
        ("SELECT pg_read_file('/etc/passwd')", "postgresql"),
        ("SELECT set_config('default_transaction_read_only','off',false)", "postgresql"),
        ("SELECT DBMS_LOCK.SLEEP(10) FROM dual", "oracle"),
        ("SELECT SLEEP(10)", "mysql"),
        ("SHOW TABLES", "mysql"),
        ("EXPLAIN SELECT * FROM users", "postgresql"),
        ("", "mysql"),
    ],
)
def test_blocks_non_readonly_or_unsupported(sql: str, dialect: str) -> None:
    with pytest.raises(SqlGuardError):
        validate_readonly_sql(sql, dialect, "app")


def test_mysql_cross_database_reference_is_blocked() -> None:
    with pytest.raises(SqlGuardError):
        validate_readonly_sql("SELECT * FROM other_db.users", "mysql", "app")


def test_mssql_cross_database_reference_is_blocked() -> None:
    with pytest.raises(SqlGuardError):
        validate_readonly_sql("SELECT * FROM other_db.dbo.users", "mssql", "app")


def test_mssql_same_database_is_allowed() -> None:
    sql = "SELECT * FROM app.dbo.users"
    assert validate_readonly_sql(sql, "mssql", "app") == sql
