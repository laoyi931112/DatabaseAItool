from __future__ import annotations

from app.adapters.base import DBConfig, DB_TYPE_INFO, AdapterError
from app.adapters.mongo import MongoAdapter
from app.adapters.sql import SQLAdapter

__all__ = [
    "AdapterError",
    "DBConfig",
    "DB_TYPE_INFO",
    "get_adapter",
]


def get_adapter(config: DBConfig):
    if config.db_type == "mongodb":
        return MongoAdapter(config)
    return SQLAdapter(config)
