from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import ApiKey, DatabaseConnection
from app.services import connections as cs


def _create_connection(client: TestClient, path: Path, name: str = "demo") -> int:
    response = client.post(
        "/databases/new",
        data={
            "name": name,
            "db_type": "sqlite",
            "host": "",
            "port": "",
            "database": str(path),
            "username": "",
            "password": "",
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
            select(DatabaseConnection).where(DatabaseConnection.name == name)
        )
        assert record is not None
        return record.id


def _create_key(client: TestClient, database_ids: list[int], name: str = "agent") -> str:
    response = client.post(
        "/keys/new",
        data={
            "name": name,
            "expires_at": "",
            "database_ids": [str(database_id) for database_id in database_ids],
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200, response.text
    match = re.search(r'id="raw-key"[^>]*>([^<]+)<', response.text)
    assert match is not None
    return match.group(1).strip()


def _initialize(client: TestClient, raw_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {raw_key}",
        "Accept": "application/json, text/event-stream",
    }
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id
    headers["Mcp-Session-Id"] = session_id
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
    )
    return headers


def _rpc(client: TestClient, headers: dict[str, str], payload: dict) -> dict:
    response = client.post("/mcp", json=payload, headers=headers)
    messages = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    if not messages:
        messages = [json.loads(response.text)]
    return messages[0]


def _call_tool(
    client: TestClient, headers: dict[str, str], name: str, arguments: dict
) -> dict:
    message = _rpc(
        client,
        headers,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return message["result"]


def test_disable_host_check(monkeypatch) -> None:
    from app.mcp import http as mcp_http

    monkeypatch.setattr(mcp_http.settings, "mcp_disable_host_check", True)
    security = mcp_http._transport_security()
    assert security.enable_dns_rebinding_protection is False


def test_database_and_key_ui_flow(client: TestClient, sqlite_file: Path) -> None:
    response = client.get("/databases")
    assert response.status_code == 200
    assert "还没有数据库连接" in response.text

    database_id = _create_connection(client, sqlite_file)

    response = client.get("/databases")
    assert "demo" in response.text
    assert "已启用" in response.text

    response = client.post(f"/databases/{database_id}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    raw_key = _create_key(client, [database_id])
    response = client.get("/keys")
    assert "agent" in response.text
    assert "demo" in response.text
    assert raw_key[:12] in response.text

    with SessionLocal() as session:
        key = session.scalar(select(ApiKey).where(ApiKey.name == "agent"))
        assert key is not None
        assert key.key_hash != raw_key
        assert cs.authenticate_api_key(session, raw_key) is not None
        stored = session.scalar(
            select(DatabaseConnection).where(DatabaseConnection.name == "demo")
        )
        assert stored is not None

    response = client.post(
        f"/keys/{key.id}/rotate", follow_redirects=False
    )
    assert response.status_code == 200
    match = re.search(r'id="raw-key"[^>]*>([^<]+)<', response.text)
    assert match is not None
    new_key = match.group(1).strip()
    assert new_key != raw_key
    with SessionLocal() as session:
        assert cs.authenticate_api_key(session, raw_key) is None
        assert cs.authenticate_api_key(session, new_key) is not None


def test_key_without_databases_has_no_access(client: TestClient) -> None:
    response = client.post(
        "/keys/new",
        data={"name": "no-db", "expires_at": "", "enabled": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    match = re.search(r'id="raw-key"[^>]*>([^<]+)<', response.text)
    assert match is not None
    raw_key = match.group(1).strip()

    with SessionLocal() as session:
        key = cs.authenticate_api_key(session, raw_key)
        assert key is not None
        assert cs.list_databases_for_key(session, key.id) == []

    headers = _initialize(client, raw_key)
    result = _call_tool(client, headers, "list_databases", {})
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["databases"] == []


def test_mcp_protocol_and_permissions(
    client: TestClient, sqlite_file: Path, tmp_path: Path
) -> None:
    allowed_id = _create_connection(client, sqlite_file, "allowed")
    other_file = tmp_path / "other.db"
    shutil.copy(sqlite_file, other_file)
    _create_connection(client, other_file, "other")
    raw_key = _create_key(client, [allowed_id], "limited")

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1})
    assert response.status_code == 401

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1},
        headers={"Authorization": "Bearer invalid", "Accept": "application/json"},
    )
    assert response.status_code == 401

    headers = _initialize(client, raw_key)
    message = _rpc(
        client, headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    tool_names = {tool["name"] for tool in message["result"]["tools"]}
    assert {"list_databases", "query", "find_documents"} <= tool_names

    result = _call_tool(client, headers, "query", {
        "database": "allowed",
        "sql": "SELECT name FROM users ORDER BY id",
        "max_rows": 2,
    })
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["rows"] == [["Alice"], ["Bob"]]
    assert payload["truncated"] is True

    result = _call_tool(client, headers, "query", {
        "database": "allowed",
        "sql": "UPDATE users SET age = 1",
    })
    assert result["isError"] is True
    assert "SELECT" in result["content"][0]["text"]

    result = _call_tool(client, headers, "query", {
        "database": "other",
        "sql": "SELECT * FROM users",
    })
    assert result["isError"] is True
    assert "无权访问" in result["content"][0]["text"]

    with SessionLocal() as session:
        key = session.scalar(select(ApiKey).where(ApiKey.name == "limited"))
        assert key is not None
        key.enabled = False
        session.commit()

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        headers=headers,
    )
    assert response.status_code == 401


def test_key_permission_edit(client: TestClient, sqlite_file: Path, tmp_path: Path) -> None:
    first_id = _create_connection(client, sqlite_file, "first")
    other_file = tmp_path / "other.db"
    shutil.copy(sqlite_file, other_file)
    second_id = _create_connection(client, other_file, "second")
    raw_key = _create_key(client, [first_id])

    with SessionLocal() as session:
        key = cs.authenticate_api_key(session, raw_key)
        assert key is not None
        key_id = key.id

    response = client.post(
        f"/keys/{key_id}/edit",
        data={
            "name": "agent",
            "expires_at": "",
            "database_ids": [str(first_id), str(second_id)],
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with SessionLocal() as session:
        key = cs.authenticate_api_key(session, raw_key)
        assert key is not None
        names = {database.name for database in cs.list_databases_for_key(session, key.id)}
        assert names == {"first", "second"}


def test_admin_basic_auth_and_csrf(
    client: TestClient, monkeypatch
) -> None:
    from app.web.main import settings

    monkeypatch.setattr(settings, "admin_password", "secret")

    assert client.get("/databases").status_code == 401

    credentials = base64.b64encode(b"admin:secret").decode("ascii")
    authorized = client.get(
        "/databases", headers={"Authorization": f"Basic {credentials}"}
    )
    assert authorized.status_code == 200

    cross_site = client.post(
        "/databases/new",
        data={"name": "x", "db_type": "sqlite", "database": "x", "enabled": "on"},
        headers={
            "Authorization": f"Basic {credentials}",
            "Origin": "http://evil.example",
        },
        follow_redirects=False,
    )
    assert cross_site.status_code == 403
