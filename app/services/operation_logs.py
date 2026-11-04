from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import ApiKey, DatabaseConnection, OperationLog
from app.utils import utcnow

PAGE_SIZE = 50
MAX_ERROR_MESSAGE = 800
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"


def build_detail(params: dict[str, Any]) -> str:
    """把工具入参压成一段可读、有长度上限的 JSON 摘要。"""
    cleaned = {key: value for key, value in params.items() if value is not None}
    try:
        text = json.dumps(cleaned, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(cleaned)
    limit = max(0, settings.operation_log_max_detail_chars)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[已截断 {len(text) - limit} 字符]"


def record(
    *,
    key_id: int | None,
    database_name: str | None,
    operation: str,
    detail: str,
    status: str,
    error_message: str | None,
    duration_ms: int,
    row_count: int | None,
) -> None:
    with SessionLocal() as session:
        key = session.get(ApiKey, key_id) if key_id is not None else None
        connection = (
            session.scalar(
                select(DatabaseConnection).where(
                    DatabaseConnection.name == database_name
                )
            )
            if database_name
            else None
        )
        session.add(
            OperationLog(
                api_key_id=key.id if key else None,
                api_key_name=key.name if key else None,
                connection_id=connection.id if connection else None,
                database_name=database_name,
                operation=operation[:50],
                detail=detail,
                status=status,
                error_message=error_message,
                duration_ms=duration_ms,
                row_count=row_count,
            )
        )
        session.commit()


def prune_expired() -> int:
    cutoff = utcnow() - timedelta(days=max(1, settings.operation_log_retention_days))
    with SessionLocal() as session:
        result = session.execute(
            delete(OperationLog).where(OperationLog.created_at < cutoff)
        )
        session.commit()
        return int(result.rowcount or 0)


def list_logs(
    session: Session,
    *,
    key_id: int | None = None,
    database: str | None = None,
    status: str | None = None,
    page: int = 1,
) -> tuple[list[OperationLog], int]:
    conditions = []
    if key_id is not None:
        conditions.append(OperationLog.api_key_id == key_id)
    if database:
        conditions.append(OperationLog.database_name == database)
    if status in (STATUS_SUCCESS, STATUS_ERROR):
        conditions.append(OperationLog.status == status)

    total = session.scalar(
        select(func.count()).select_from(OperationLog).where(*conditions)
    )
    page = max(1, page)
    records = list(
        session.scalars(
            select(OperationLog)
            .where(*conditions)
            .order_by(OperationLog.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        ).all()
    )
    return records, int(total or 0)


def database_names(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(OperationLog.database_name)
            .where(OperationLog.database_name.is_not(None))
            .distinct()
            .order_by(OperationLog.database_name)
        ).all()
    )


def clear_logs(session: Session) -> int:
    result = session.execute(delete(OperationLog))
    session.commit()
    return int(result.rowcount or 0)
