from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="databaseaitool-tests-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["MCP_ALLOWED_HOSTS"] = "testserver,testserver:*"
os.environ["ADMIN_PASSWORD"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import SessionLocal, init_db
from app.models import ApiKey, DatabaseConnection, key_database_permissions


@pytest.fixture(scope="session")
def client():
    init_db()

    from app.web.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database():
    init_db()
    with SessionLocal() as session:
        session.execute(delete(key_database_permissions))
        session.execute(delete(ApiKey))
        session.execute(delete(DatabaseConnection))
        session.commit()
    yield


@pytest.fixture()
def sqlite_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER)"
    )
    connection.executemany(
        "INSERT INTO users(name, age) VALUES (?, ?)",
        [("Alice", 30), ("Bob", 25), ("Carol", 41)],
    )
    connection.commit()
    connection.close()
    return path
