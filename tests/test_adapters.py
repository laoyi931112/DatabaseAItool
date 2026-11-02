from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.adapters.base import AdapterError, DBConfig, jsonable
from app.adapters.mongo import MongoAdapter
from app.adapters.sql import SQLAdapter
from app.db import SessionLocal
from app.services import connections as cs


def _config(sqlite_file: Path) -> DBConfig:
    with SessionLocal() as session:
        record = cs.create_database(
            session,
            name="adapter-test",
            db_type="sqlite",
            database=str(sqlite_file),
            max_rows=10,
            query_timeout=5,
        )
        return DBConfig.from_model(record)


def test_sqlite_query_is_limited_and_read_only(sqlite_file: Path) -> None:
    adapter = SQLAdapter(_config(sqlite_file))

    result = adapter.query("SELECT id, name FROM users ORDER BY id", max_rows=2)
    assert result["columns"] == ["id", "name"]
    assert result["row_count"] == 2
    assert result["truncated"] is True

    with pytest.raises(AdapterError):
        adapter.query("UPDATE users SET age = 1")

    engine = adapter._create_sqlite_engine()
    try:
        with pytest.raises(Exception, match="readonly"):
            with engine.connect() as connection:
                connection.execute(text("INSERT INTO users(name) VALUES ('Eve')"))
                connection.commit()
    finally:
        engine.dispose()


def test_metadata_reads(sqlite_file: Path) -> None:
    adapter = SQLAdapter(_config(sqlite_file))
    assert adapter.get_database_info()["read_only"] is True
    assert {"name": "users", "type": "table"} in adapter.list_tables()
    description = adapter.describe_table("users")
    assert [column["name"] for column in description["columns"]] == [
        "id",
        "name",
        "age",
    ]
    assert description["primary_key"] == ["id"]


def test_jsonable_values() -> None:
    assert jsonable(Decimal("1.50")) == "1.50"
    assert jsonable(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert jsonable(b"abc") == "YWJj"
    assert jsonable({"a": [Decimal("2")]}) == {"a": ["2"]}


def test_mongo_rejects_write_stages_before_connecting() -> None:
    config = DBConfig(
        id=1,
        name="mongo",
        db_type="mongodb",
        database="demo",
        host="127.0.0.1",
        port=27017,
        username=None,
        password=None,
        options={},
        max_rows=10,
        query_timeout=5,
    )
    adapter = MongoAdapter(config)

    with pytest.raises(AdapterError, match=r"\$out|\$merge"):
        adapter.aggregate_documents("users", [{"$out": "archive"}])
    with pytest.raises(AdapterError, match=r"\$out|\$merge"):
        adapter.aggregate_documents("users", [{"$merge": {"into": "archive"}}])
    with pytest.raises(AdapterError):
        adapter.find_documents("users", filter=["not", "a", "dict"])
    with pytest.raises(AdapterError):
        adapter.query("SELECT 1")
