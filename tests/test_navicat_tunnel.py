from __future__ import annotations

import base64
import struct

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adapters import get_adapter
from app.adapters.base import AdapterError, DBConfig
from app.adapters.navicat_tunnel import (
    NavicatTunnelAdapter,
    _pg_server_version,
)
from app.db import SessionLocal
from app.models import DatabaseConnection
from app.services import connections as cs


def _u16(value: int) -> bytes:
    return struct.pack(">H", value)


def _u32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def _block(value: str | bytes | None) -> bytes:
    if value is None:
        return b"\xFF"
    data = value.encode("utf-8") if isinstance(value, str) else value
    if len(data) < 254:
        return bytes([len(data)]) + data
    return b"\xFE" + _u32(len(data)) + data


def _header(errno: int = 0, version: int = 206) -> bytes:
    return _u32(1111) + _u16(version) + _u32(errno) + b"\x00" * 6


def _result(
    *,
    errno: int = 0,
    columns: list[str] | None = None,
    rows: list[list[str | None]] | None = None,
    info: str = "",
    error: str = "",
) -> bytes:
    columns = columns or []
    rows = rows or []
    payload = (
        _u32(errno)
        + _u32(0)
        + _u32(0)
        + _u32(len(columns))
        + _u32(len(rows))
        + b"\x00" * 12
    )
    if errno:
        return payload + _block(error)
    if not columns:
        return payload + _block(info)
    for name in columns:
        payload += _block(name) + _block("") + _u32(3) + _u32(0) + _u32(11)
    for row in rows:
        for value in row:
            payload += _block(value)
    return payload


def _config(**overrides) -> DBConfig:
    values = {
        "id": 1,
        "name": "tunneled",
        "db_type": "mysql",
        "database": "app",
        "host": "db.internal",
        "port": 3306,
        "username": "reader",
        "password": "secret",
        "options": {},
        "max_rows": 100,
        "query_timeout": 10,
        "tunnel_url": "https://example.com/ntunnel_mysql.php",
        "tunnel_username": None,
        "tunnel_password": None,
        "tunnel_encode_base64": True,
    }
    values.update(overrides)
    return DBConfig(**values)


def test_get_adapter_uses_tunnel_and_rejects_unsupported_types() -> None:
    assert isinstance(get_adapter(_config()), NavicatTunnelAdapter)

    with pytest.raises(AdapterError, match="MySQL/MariaDB"):
        get_adapter(_config(db_type="sqlite"))


def test_connection_info_is_parsed(monkeypatch) -> None:
    adapter = NavicatTunnelAdapter(_config())
    calls: list[list[tuple[str, str]]] = []

    def fake_post(data):
        calls.append(data)
        return (
            _header()
            + _block("db.internal via TCP/IP")
            + _block("10")
            + _block("8.0.36")
        )

    monkeypatch.setattr(adapter, "_post", fake_post)

    result = adapter.test_connection()
    assert result["ok"] is True
    assert result["server_version"] == "8.0.36"
    assert ("actn", "C") in calls[0]
    assert ("host", "db.internal") in calls[0]


def test_connection_error_uses_script_message(monkeypatch) -> None:
    adapter = NavicatTunnelAdapter(_config())
    monkeypatch.setattr(
        adapter,
        "_post",
        lambda data: _header(1045) + _block("Access denied for user"),
    )

    with pytest.raises(AdapterError, match="Access denied"):
        adapter.test_connection()


def test_query_uses_readonly_session_wrap_and_base64(monkeypatch) -> None:
    adapter = NavicatTunnelAdapter(_config(max_rows=5))
    captured: dict = {}

    def fake_post(data):
        captured["data"] = data
        return (
            _header()
            + _result(info="")
            + b"\x01"
            + _result(
                columns=["id", "name"],
                rows=[["1", "Alice"], ["2", None], ["3", "Carol"]],
            )
            + b"\x00"
        )

    monkeypatch.setattr(adapter, "_post", fake_post)

    result = adapter.query("SELECT id, name FROM users ORDER BY id", max_rows=2)

    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [["1", "Alice"], ["2", None]]
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert result["max_rows"] == 2

    data = captured["data"]
    assert ("encodeBase64", "1") in data
    queries = [
        base64.b64decode(value).decode("utf-8")
        for key, value in data
        if key == "q[]"
    ]
    assert queries[0] == "SET SESSION TRANSACTION READ ONLY"
    assert "LIMIT 3" in queries[1]
    assert "FROM (SELECT id, name FROM users ORDER BY id) AS _aitool" in queries[1]


def test_query_rejects_writes_before_any_http_request(monkeypatch) -> None:
    adapter = NavicatTunnelAdapter(_config())
    called = False

    def fake_post(data):
        nonlocal called
        called = True
        return b""

    monkeypatch.setattr(adapter, "_post", fake_post)

    with pytest.raises(AdapterError, match="SELECT"):
        adapter.query("DELETE FROM users")
    assert called is False


def test_query_error_is_reported(monkeypatch) -> None:
    adapter = NavicatTunnelAdapter(_config())

    monkeypatch.setattr(
        adapter,
        "_post",
        lambda data: (
            _header()
            + _result(info="")
            + b"\x01"
            + _result(errno=1146, error="Table 'app.missing' doesn't exist")
            + b"\x00"
        ),
    )

    with pytest.raises(AdapterError, match="missing"):
        adapter.query("SELECT * FROM missing")


def test_invalid_script_response_is_reported(monkeypatch) -> None:
    adapter = NavicatTunnelAdapter(_config())
    monkeypatch.setattr(adapter, "_post", lambda data: b"<html>login</html>")

    with pytest.raises(AdapterError, match="ntunnel"):
        adapter.test_connection()


def test_postgresql_version_format() -> None:
    assert _pg_server_version("160002") == "16.2"
    assert _pg_server_version("100001") == "10.1"
    assert _pg_server_version("90624") == "9.6.24"


def test_service_persists_tunnel_fields() -> None:
    with SessionLocal() as session:
        record = cs.create_database(
            session,
            name="pg-tunnel",
            db_type="postgresql",
            database="app",
            host="db.internal",
            port="",
            username="reader",
            password="db-password",
            tunnel_url="https://example.com/ntunnel_pgsql.php",
            tunnel_username="web",
            tunnel_password="web-password",
            tunnel_encode_base64=True,
            max_rows=10,
            query_timeout=5,
        )
        assert record.port == 5432
        assert record.tunnel_url == "https://example.com/ntunnel_pgsql.php"
        assert record.tunnel_password_encrypted != "web-password"

        config = DBConfig.from_model(record)
        assert config.tunnel_password == "web-password"
        assert config.tunnel_encode_base64 is True

        cs.update_database(
            session,
            record,
            name="pg-tunnel",
            db_type="postgresql",
            database="app",
            host="db.internal",
            port="5432",
            username="reader",
            tunnel_url="https://example.com/ntunnel_pgsql.php",
            tunnel_username="web",
            max_rows=10,
            query_timeout=5,
        )
        assert record.tunnel_password_encrypted is not None

        cs.update_database(
            session,
            record,
            name="pg-tunnel",
            db_type="postgresql",
            database="app",
            host="db.internal",
            port="5432",
            username="reader",
            tunnel_url="",
            max_rows=10,
            query_timeout=5,
        )
        assert record.tunnel_url is None
        assert record.tunnel_username is None
        assert record.tunnel_password_encrypted is None


def test_service_rejects_invalid_tunnel_settings() -> None:
    with SessionLocal() as session:
        with pytest.raises(cs.ServiceError, match="http"):
            cs.create_database(
                session,
                name="bad-url",
                db_type="mysql",
                database="app",
                tunnel_url="example.com/tunnel.php",
            )

        with pytest.raises(cs.ServiceError, match="MySQL/MariaDB"):
            cs.create_database(
                session,
                name="bad-type",
                db_type="sqlite",
                database="/tmp/demo.db",
                tunnel_url="https://example.com/ntunnel_sqlite.php",
            )


def test_web_form_saves_tunnel_fields(client: TestClient) -> None:
    response = client.post(
        "/databases/new",
        data={
            "name": "web-tunnel",
            "db_type": "mysql",
            "host": "db.internal",
            "port": "",
            "database": "app",
            "username": "reader",
            "password": "db-password",
            "tunnel_url": "https://example.com/ntunnel_mysql.php",
            "tunnel_username": "web",
            "tunnel_password": "web-password",
            "tunnel_encode_base64": "on",
            "options": "",
            "description": "",
            "enabled": "on",
            "max_rows": "100",
            "query_timeout": "10",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with SessionLocal() as session:
        record = session.scalar(
            select(DatabaseConnection).where(DatabaseConnection.name == "web-tunnel")
        )
        assert record is not None
        assert record.tunnel_url == "https://example.com/ntunnel_mysql.php"
        assert record.tunnel_username == "web"
        assert record.tunnel_password_encrypted is not None
        assert record.tunnel_encode_base64 is True
        assert record.port == 3306
