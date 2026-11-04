from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_web_mcp import _call_tool, _create_connection, _create_key, _initialize

from app.config import settings
from app.db import SessionLocal
from app.mcp import service
from app.models import DatabaseConnection, OperationLog
from app.services import connections as cs
from app.services import operation_logs as als
from app.utils import utcnow


def _records() -> list[OperationLog]:
    with SessionLocal() as session:
        return list(
            session.scalars(select(OperationLog).order_by(OperationLog.id)).all()
        )


def _authorized_client(client: TestClient, sqlite_file: Path) -> dict[str, str]:
    database_id = _create_connection(client, sqlite_file, "demo")
    raw_key = _create_key(client, [database_id], "agent")
    return _initialize(client, raw_key)


def test_successful_query_is_logged(
    client: TestClient, sqlite_file: Path
) -> None:
    headers = _authorized_client(client, sqlite_file)
    result = _call_tool(
        client,
        headers,
        "query",
        {
            "database": "demo",
            "sql": "SELECT name FROM users ORDER BY id",
            "max_rows": 2,
        },
    )
    assert result["isError"] is False

    records = _records()
    assert len(records) == 1
    log = records[0]
    assert log.operation == "query"
    assert log.status == als.STATUS_SUCCESS
    assert log.api_key_name == "agent"
    assert log.api_key_id is not None
    assert log.database_name == "demo"
    assert log.row_count == 2
    assert log.duration_ms is not None and log.duration_ms >= 0
    assert log.error_message is None
    assert "SELECT name FROM users" in log.detail
    assert "key_id" not in log.detail


def test_rejected_write_is_logged_as_error(
    client: TestClient, sqlite_file: Path
) -> None:
    headers = _authorized_client(client, sqlite_file)
    result = _call_tool(
        client,
        headers,
        "query",
        {"database": "demo", "sql": "UPDATE users SET age = 1"},
    )
    assert result["isError"] is True

    log = _records()[-1]
    assert log.status == als.STATUS_ERROR
    assert log.row_count is None
    assert "SELECT" in (log.error_message or "")


def test_unauthorized_database_is_logged(
    client: TestClient, sqlite_file: Path, tmp_path: Path
) -> None:
    headers = _authorized_client(client, sqlite_file)
    result = _call_tool(
        client,
        headers,
        "list_tables",
        {"database": "secret"},
    )
    assert result["isError"] is True

    log = _records()[-1]
    assert log.status == als.STATUS_ERROR
    assert log.database_name == "secret"
    assert log.connection_id is None
    assert "无权访问" in (log.error_message or "")


def test_detail_is_truncated(
    client: TestClient, sqlite_file: Path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "operation_log_max_detail_chars", 50)
    headers = _authorized_client(client, sqlite_file)
    long_sql = "SELECT '" + "x" * 400 + "'"
    _call_tool(client, headers, "query", {"database": "demo", "sql": long_sql})

    log = _records()[-1]
    assert "已截断" in log.detail
    assert len(log.detail) < 100


def test_direct_service_call_is_logged(client: TestClient, sqlite_file: Path) -> None:
    database_id = _create_connection(client, sqlite_file, "demo")
    raw_key = _create_key(client, [database_id], "agent")
    with SessionLocal() as session:
        key = cs.authenticate_api_key(session, raw_key)
        assert key is not None
        key_id = key.id

    service.list_tables(key_id, "demo")

    log = _records()[0]
    assert log.operation == "list_tables"
    assert log.database_name == "demo"
    assert log.api_key_id == key_id


def test_log_keeps_names_after_target_is_deleted(
    client: TestClient, sqlite_file: Path
) -> None:
    database_id = _create_connection(client, sqlite_file, "demo")
    raw_key = _create_key(client, [database_id], "agent")
    with SessionLocal() as session:
        key = cs.authenticate_api_key(session, raw_key)
        assert key is not None
        key_id = key.id

    service.query(key_id, "demo", "SELECT 1")
    assert len(_records()) == 1

    with SessionLocal() as session:
        cs.delete_database(session, session.get(DatabaseConnection, database_id))

    log = _records()[0]
    assert log.database_name == "demo"
    assert log.connection_id is None
    assert log.api_key_name == "agent"
    assert log.api_key_id == key_id


def test_logging_failure_does_not_break_tool_call(
    client: TestClient, sqlite_file: Path, monkeypatch
) -> None:
    def explode(**_kwargs: object) -> None:
        raise RuntimeError("日志写入失败")

    monkeypatch.setattr(als, "record", explode)
    headers = _authorized_client(client, sqlite_file)
    result = _call_tool(
        client,
        headers,
        "query",
        {"database": "demo", "sql": "SELECT name FROM users"},
    )
    assert result["isError"] is False
    assert _records() == []


def test_expired_logs_are_pruned() -> None:
    with SessionLocal() as session:
        session.add(
            OperationLog(
                created_at=utcnow() - timedelta(
                    days=settings.operation_log_retention_days + 10
                ),
                operation="query",
                status=als.STATUS_SUCCESS,
                detail="{}",
            )
        )
        session.add(
            OperationLog(
                created_at=utcnow(),
                operation="query",
                status=als.STATUS_SUCCESS,
                detail="{}",
            )
        )
        session.commit()

    assert als.prune_expired() == 1
    assert len(_records()) == 1


def test_logs_page_filters_and_clear(client: TestClient, sqlite_file: Path) -> None:
    headers = _authorized_client(client, sqlite_file)
    _call_tool(
        client,
        headers,
        "query",
        {"database": "demo", "sql": "SELECT name FROM users ORDER BY id"},
    )
    _call_tool(
        client,
        headers,
        "query",
        {"database": "demo", "sql": "DELETE FROM users"},
    )

    response = client.get("/logs")
    assert response.status_code == 200
    assert "共 2 条" in response.text
    assert "SELECT name FROM users" in response.text
    assert "DELETE FROM users" in response.text

    response = client.get("/logs?status=error")
    assert response.status_code == 200
    assert "共 1 条" in response.text
    assert "SELECT name FROM users" not in response.text

    response = client.get("/logs?database=demo&status=success")
    assert "共 1 条" in response.text

    response = client.post("/logs/clear", follow_redirects=False)
    assert response.status_code == 303
    assert _records() == []

    response = client.get("/logs")
    assert "没有符合条件的日志" in response.text
